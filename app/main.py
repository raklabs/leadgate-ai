"""FastAPI application entrypoint, REST API routes, security middleware, and operator review dashboard."""

from __future__ import annotations

import html
import uuid
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import Settings, get_settings
from app.domain.models import (
    EmailApprovalRequest,
    EnquiryCreateRequest,
    LeadQualificationResponse,
)
from app.domain.scorer import RuleBasedLeadScorer
from app.repositories.enquiry_repo import EnquiryRepository
from app.security.auth import require_api_key, verify_webhook_request
from app.security.rate_limit import SlidingWindowRateLimiter
from app.services.crm_client import build_crm_client
from app.services.email_sender import build_email_sender
from app.services.llm_extractor import build_extractor
from app.services.pipeline import LeadQualificationPipeline


@lru_cache(maxsize=1)
def get_pipeline() -> LeadQualificationPipeline:
    """Construct default singleton LeadQualificationPipeline from environment settings."""
    settings = get_settings()
    repository = EnquiryRepository(db_path=settings.DATABASE_PATH)
    extractor = build_extractor(settings)
    scorer = RuleBasedLeadScorer.from_yaml(settings.SCORING_RULES_PATH)
    crm_client = build_crm_client(settings)
    email_sender = build_email_sender(settings)
    return LeadQualificationPipeline(
        repository=repository,
        extractor=extractor,
        scorer=scorer,
        crm_client=crm_client,
        dedup_window_hours=settings.DEDUP_WINDOW_HOURS,
        email_sender=email_sender,
    )


def create_app(
    pipeline_override: LeadQualificationPipeline | None = None,
    rate_limit_rpm: int | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title="IntelligentStack AI — Lead Qualification & CRM Sync Engine",
        version="0.1.0",
        description="Spec-Driven Phase 1 AI Lead Qualification Pipeline (Zero-Guessing Extraction + YAML Rule Scoring + CRM Upsert)",
    )

    if pipeline_override is not None:
        app.dependency_overrides[get_pipeline] = lambda: pipeline_override

    rpm = rate_limit_rpm if rate_limit_rpm is not None else settings.RATE_LIMIT_REQUESTS_PER_MINUTE
    limiter = SlidingWindowRateLimiter(max_requests=rpm, window_seconds=60)

    @app.middleware("http")
    async def security_headers_and_rate_limit_middleware(request: Request, call_next: Any) -> Response:
        correlation_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"

        if request.url.path == "/api/v1/enquiries/webhook" and request.method == "POST":
            client_host = request.client.host if request.client else "local"
            allowed, retry_after = limiter.allow_request(client_host)
            if not allowed:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "Rate limit exceeded. Please retry later."},
                    headers={
                        "Retry-After": str(retry_after),
                        "X-Request-ID": correlation_id,
                    },
                )

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'"
        )
        return response

    @app.get("/healthz", tags=["System"])
    async def healthcheck(
        cfg: Settings = Depends(get_settings),
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
    ) -> dict[str, Any]:
        """Health check reporting database connectivity, active LLM provider, and CRM configuration."""
        return {
            "status": "ok",
            "environment": cfg.APP_ENV,
            "llm_provider": cfg.LLM_PROVIDER,
            "crm_provider": cfg.CRM_PROVIDER,
            "email_provider": cfg.EMAIL_PROVIDER,
            "scoring_rules_version": pipeline.scorer.rules.version,
        }

    @app.post(
        "/api/v1/enquiries/webhook",
        response_model=LeadQualificationResponse,
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(verify_webhook_request)],
        tags=["Enquiries"],
    )
    async def ingest_enquiry_webhook(
        payload: EnquiryCreateRequest,
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
    ) -> LeadQualificationResponse:
        """Ingest a web form or inbound email enquiry, qualify it, and sync with CRM."""
        return await pipeline.process_enquiry(payload)

    @app.get(
        "/api/v1/enquiries",
        response_model=list[LeadQualificationResponse],
        dependencies=[Depends(require_api_key)],
        tags=["Enquiries"],
    )
    async def list_enquiries(
        tier: str | None = Query(default=None, description="Filter by Hot, Warm, Medium, or Cold"),
        needs_review: bool | None = Query(default=None, description="Filter by needs_review flag"),
        limit: int = Query(default=50, ge=1, le=200),
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
    ) -> list[LeadQualificationResponse]:
        """List processed enquiries with optional tier and review status filters."""
        return pipeline.repository.list_enquiries(tier=tier, needs_review=needs_review, limit=limit)

    @app.get(
        "/api/v1/enquiries/{enquiry_id}",
        response_model=LeadQualificationResponse,
        dependencies=[Depends(require_api_key)],
        tags=["Enquiries"],
    )
    async def get_enquiry(
        enquiry_id: str,
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
    ) -> LeadQualificationResponse:
        """Fetch full qualification details, score breakdown, CRM status, and draft email."""
        record = pipeline.repository.get_by_id(enquiry_id)
        if not record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enquiry not found.")
        return record

    @app.post(
        "/api/v1/enquiries/{enquiry_id}/approve-email",
        response_model=LeadQualificationResponse,
        dependencies=[Depends(require_api_key)],
        tags=["Enquiries"],
    )
    async def approve_acknowledgement_email(
        enquiry_id: str,
        approval: EmailApprovalRequest,
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
    ) -> LeadQualificationResponse:
        """Approve (and optionally edit) the AI-drafted acknowledgement email and dispatch it."""
        updated = await pipeline.approve_and_send_email(
            enquiry_id=enquiry_id,
            reviewer_name=approval.reviewer_name,
            subject=approval.subject,
            body=approval.body,
        )
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enquiry not found.")
        return updated

    @app.post(
        "/api/v1/enquiries/{enquiry_id}/retry-sync",
        response_model=LeadQualificationResponse,
        dependencies=[Depends(require_api_key)],
        tags=["Enquiries"],
    )
    async def retry_crm_sync(
        enquiry_id: str,
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
    ) -> LeadQualificationResponse:
        """Re-run CRM synchronization for an enquiry that previously failed sync."""
        updated = await pipeline.retry_crm_sync(enquiry_id)
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enquiry not found.")
        return updated

    @app.get("/dashboard", response_class=HTMLResponse, tags=["Dashboard"])
    async def operator_dashboard(
        tier: str | None = Query(default=None),
        needs_review: bool | None = Query(default=None),
        pipeline: LeadQualificationPipeline = Depends(get_pipeline),
        cfg: Settings = Depends(get_settings),
    ) -> HTMLResponse:
        """Interactive operator review dashboard with safe HTML escaping, email approval, and live simulator."""
        enquiries = pipeline.repository.list_enquiries(tier=tier, needs_review=needs_review, limit=100)
        rows_html: list[str] = []

        tier_colors = {
            "Hot": "#dc2626",
            "Warm": "#ea580c",
            "Medium": "#2563eb",
            "Cold": "#64748b",
        }

        for item in enquiries:
            ext = item.extracted
            sc = item.scoring
            crm = item.crm_sync
            tier_val = sc.tier.value if sc else "N/A"
            score_val = str(sc.total_score) if sc else "-"
            badge_color = tier_colors.get(tier_val, "#475569")
            enq_id_esc = html.escape(item.enquiry_id)

            contact_str = html.escape(
                f"{ext.contact_name or 'Unknown'} ({ext.contact_email or ext.contact_phone or 'No contact'})"
                if ext
                else "Extraction Failed"
            )
            service_str = html.escape(
                ext.service_or_property_type.value if ext and ext.service_or_property_type else "None (Null)"
            )
            budget_str = (
                html.escape(f"{ext.budget_currency or '$'} {ext.budget_max:,.0f}")
                if ext and ext.budget_max is not None
                else "<em>None (Null)</em>"
            )
            timeline_str = html.escape(ext.timeline.value if ext and ext.timeline else "None (Null)")
            confidence_str = f"{ext.extraction_confidence:.2f}" if ext else "0.00"
            review_badge = (
                '<span style="background:#fef2f2;color:#b91c1c;padding:2px 8px;border-radius:999px;font-weight:600;font-size:12px;">NEEDS REVIEW</span>'
                if item.needs_review
                else '<span style="background:#f0fdf4;color:#15803d;padding:2px 8px;border-radius:999px;font-weight:600;font-size:12px;">VERIFIED</span>'
            )
            reasons_html = (
                "<ul style='margin:4px 0 0 16px;padding:0;font-size:12px;color:#b91c1c;'>"
                + "".join(f"<li>{html.escape(r)}</li>" for r in item.review_reasons)
                + "</ul>"
                if item.review_reasons
                else ""
            )
            crm_str = html.escape(
                f"{crm.status.value} ({crm.crm_record_id or 'no-id'}) [attempts={crm.attempts}]"
                if crm
                else "Not synced"
            )
            summary_str = html.escape(ext.ai_summary if ext else "")
            draft_subj = html.escape(ext.draft_acknowledgement_subject if ext else "")
            draft_body = html.escape(ext.draft_acknowledgement_body if ext else "")
            approved_str = (
                f"<br/><span style='color:#15803d;font-size:11px;'>Approved by {html.escape(item.approved_by)}</span>"
                if item.approved_by
                else ""
            )

            rows_html.append(
                f"""
                <tr style="border-bottom:1px solid #e2e8f0;">
                    <td style="padding:12px;vertical-align:top;">
                        <strong>{enq_id_esc}</strong><br/>
                        <span style="font-size:12px;color:#64748b;">{html.escape(item.source.value)}</span>
                    </td>
                    <td style="padding:12px;vertical-align:top;">{contact_str}</td>
                    <td style="padding:12px;vertical-align:top;">
                        <span style="background:{badge_color};color:white;padding:3px 10px;border-radius:999px;font-weight:700;font-size:12px;">
                            {html.escape(tier_val)} ({html.escape(score_val)})
                        </span>
                    </td>
                    <td style="padding:12px;vertical-align:top;font-size:13px;">
                        <strong>Service:</strong> {service_str}<br/>
                        <strong>Budget:</strong> {budget_str}<br/>
                        <strong>Timeline:</strong> {timeline_str}<br/>
                        <strong>Confidence:</strong> {confidence_str}
                    </td>
                    <td style="padding:12px;vertical-align:top;">
                        {review_badge}
                        {reasons_html}
                    </td>
                    <td style="padding:12px;vertical-align:top;font-size:13px;">
                        {crm_str}<br/>
                        <strong>Email:</strong> {html.escape(item.email_draft_status.value)}{approved_str}<br/>
                        <button onclick="retrySync('{enq_id_esc}')" style="margin-top:6px;padding:3px 8px;font-size:11px;cursor:pointer;border:1px solid #cbd5e1;background:#f8fafc;border-radius:4px;">Retry CRM Sync</button>
                    </td>
                    <td style="padding:12px;vertical-align:top;font-size:13px;max-width:360px;">
                        <div style="margin-bottom:6px;">{summary_str}</div>
                        <details>
                            <summary style="cursor:pointer;color:#2563eb;font-weight:600;">Review &amp; Approve Draft Email</summary>
                            <div style="background:#f8fafc;padding:10px;border:1px solid #cbd5e1;border-radius:6px;margin-top:6px;">
                                <label style="font-size:11px;font-weight:600;display:block;">Subject</label>
                                <input id="subj_{enq_id_esc}" value="{draft_subj}" style="width:96%;margin-bottom:6px;padding:4px;font-size:12px;"/>
                                <label style="font-size:11px;font-weight:600;display:block;">Body</label>
                                <textarea id="body_{enq_id_esc}" rows="5" style="width:96%;padding:4px;font-size:12px;">{draft_body}</textarea>
                                <button onclick="approveEmail('{enq_id_esc}')" style="margin-top:6px;background:#16a34a;color:white;border:none;padding:5px 10px;border-radius:4px;cursor:pointer;font-weight:600;font-size:12px;">Approve &amp; Send Email</button>
                            </div>
                        </details>
                    </td>
                </tr>
                """
            )

        default_key = html.escape(cfg.API_KEY.get_secret_value())
        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"/>
    <title>IntelligentStack AI — Lead Qualification Dashboard</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;background:#f8fafc;color:#0f172a;">
    <div style="max-width:1420px;margin:0 auto;padding:24px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
            <div>
                <div style="font-size:12px;font-weight:700;color:#2563eb;text-transform:uppercase;letter-spacing:0.06em;">IntelligentStack AI &bull; Reference Architecture</div>
                <h1 style="margin:4px 0 0 0;font-size:24px;">AI Lead Qualification &amp; CRM Sync Dashboard</h1>
                <p style="margin:4px 0 0 0;color:#475569;font-size:14px;">
                    LLM Provider: <code>{html.escape(cfg.LLM_PROVIDER)}</code> &bull; CRM Adapter: <code>{html.escape(cfg.CRM_PROVIDER)}</code> &bull; Zero-Guessing Quote Verifier Active
                </p>
            </div>
            <div style="display:flex;gap:8px;font-size:13px;">
                <a href="/dashboard" style="padding:6px 12px;background:#e2e8f0;border-radius:6px;text-decoration:none;color:#0f172a;font-weight:600;">All ({len(enquiries)})</a>
                <a href="/dashboard?tier=Hot" style="padding:6px 12px;background:#fee2e2;border-radius:6px;text-decoration:none;color:#991b1b;font-weight:600;">Hot</a>
                <a href="/dashboard?tier=Warm" style="padding:6px 12px;background:#ffedd5;border-radius:6px;text-decoration:none;color:#9a3412;font-weight:600;">Warm</a>
                <a href="/dashboard?tier=Medium" style="padding:6px 12px;background:#dbeafe;border-radius:6px;text-decoration:none;color:#1e40af;font-weight:600;">Medium</a>
                <a href="/dashboard?tier=Cold" style="padding:6px 12px;background:#f1f5f9;border-radius:6px;text-decoration:none;color:#334155;font-weight:600;">Cold</a>
                <a href="/dashboard?needs_review=true" style="padding:6px 12px;background:#fef3c7;border-radius:6px;text-decoration:none;color:#92400e;font-weight:600;">Needs Review</a>
            </div>
        </div>

        <details style="background:white;padding:14px 18px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.08);margin-bottom:18px;">
            <summary style="cursor:pointer;font-weight:700;color:#0f172a;font-size:14px;">+ Simulate Live Inbound Enquiry (Web Form / Email Webhook)</summary>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-top:12px;">
                <input id="sim_name" placeholder="Sender Name (e.g. Jane Doe)" style="padding:6px;border:1px solid #cbd5e1;border-radius:4px;"/>
                <input id="sim_email" placeholder="Sender Email" style="padding:6px;border:1px solid #cbd5e1;border-radius:4px;"/>
                <input id="sim_phone" placeholder="Sender Phone" style="padding:6px;border:1px solid #cbd5e1;border-radius:4px;"/>
                <input id="sim_subject" placeholder="Subject Line" style="padding:6px;border:1px solid #cbd5e1;border-radius:4px;"/>
            </div>
            <textarea id="sim_body" rows="3" placeholder="Paste customer enquiry message body..." style="width:99%;margin-top:10px;padding:6px;border:1px solid #cbd5e1;border-radius:4px;"></textarea>
            <div style="margin-top:8px;">
                <button onclick="submitEnquiry()" style="background:#2563eb;color:white;border:none;padding:7px 14px;border-radius:6px;cursor:pointer;font-weight:600;">Qualify &amp; Sync Lead</button>
                <span id="sim_status" style="margin-left:12px;font-size:13px;color:#475569;"></span>
            </div>
        </details>

        <div style="background:white;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.08);overflow-x:auto;">
            <table style="width:100%;border-collapse:collapse;text-align:left;">
                <thead>
                    <tr style="background:#f1f5f9;border-bottom:2px solid #cbd5e1;font-size:13px;color:#334155;">
                        <th style="padding:12px;">Enquiry ID</th>
                        <th style="padding:12px;">Contact</th>
                        <th style="padding:12px;">Score &amp; Tier</th>
                        <th style="padding:12px;">Extracted Fields (Null if Unstated)</th>
                        <th style="padding:12px;">Governance</th>
                        <th style="padding:12px;">CRM &amp; Email Status</th>
                        <th style="padding:12px;">AI Summary &amp; Human-in-the-Loop Email</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(rows_html) if rows_html else '<tr><td colspan="7" style="padding:24px;text-align:center;color:#64748b;">No enquiries processed yet. Run scripts/evaluate_20_enquiries.py or use the simulator above.</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
    <script>
        const API_KEY = "{default_key}";
        async function approveEmail(enqId) {{
            const subj = document.getElementById("subj_" + enqId).value;
            const body = document.getElementById("body_" + enqId).value;
            const resp = await fetch("/api/v1/enquiries/" + enqId + "/approve-email", {{
                method: "POST",
                headers: {{"Content-Type": "application/json", "X-API-Key": API_KEY}},
                body: JSON.stringify({{reviewer_name: "Operator Dashboard", subject: subj, body: body}})
            }});
            if (resp.ok) {{ window.location.reload(); }}
            else {{ alert("Failed to approve email: " + await resp.text()); }}
        }}
        async function retrySync(enqId) {{
            const resp = await fetch("/api/v1/enquiries/" + enqId + "/retry-sync", {{
                method: "POST",
                headers: {{"X-API-Key": API_KEY}}
            }});
            if (resp.ok) {{ window.location.reload(); }}
            else {{ alert("Retry failed: " + await resp.text()); }}
        }}
        async function submitEnquiry() {{
            const st = document.getElementById("sim_status");
            st.textContent = "Processing via AI pipeline...";
            const payload = {{
                source: "web_form",
                sender_name: document.getElementById("sim_name").value || null,
                sender_email: document.getElementById("sim_email").value || null,
                sender_phone: document.getElementById("sim_phone").value || null,
                subject: document.getElementById("sim_subject").value || null,
                message_body: document.getElementById("sim_body").value
            }};
            const resp = await fetch("/api/v1/enquiries/webhook", {{
                method: "POST",
                headers: {{"Content-Type": "application/json", "X-API-Key": API_KEY}},
                body: JSON.stringify(payload)
            }});
            if (resp.ok) {{ window.location.reload(); }}
            else {{ st.textContent = "Error: " + await resp.text(); }}
        }}
    </script>
</body>
</html>"""
        return HTMLResponse(content=page)

    return app


app = create_app()
