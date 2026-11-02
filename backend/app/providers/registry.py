"""Builds one runtime entry per provider from settings.

A provider with missing configuration gets ``adapter=None`` and status NOT_CONFIGURED
(listing the variable *names* it needs) instead of raising, so the rest keeps working.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from app.core.config import Settings
from app.providers.aws import AwsAdapter, ClientFactory, default_client_factory
from app.providers.base import CapabilityInfo, ProviderAdapter
from app.providers.deepgram import DeepgramAdapter
from app.providers.elevenlabs import ElevenLabsAdapter
from app.providers.http import Sleep
from app.providers.openai import OpenAIAdapter
from app.providers.sarvam import SarvamAdapter
from app.providers.types import Operation, SyncStatus

logger = logging.getLogger(__name__)

ADAPTER_CLASSES: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAIAdapter,
    "sarvam": SarvamAdapter,
    "deepgram": DeepgramAdapter,
    "elevenlabs": ElevenLabsAdapter,
    "aws": AwsAdapter,
}


@dataclass
class ProviderRuntime:
    slug: str
    adapter: ProviderAdapter | None
    status: SyncStatus  # OK (configured), NOT_CONFIGURED or DISABLED
    missing_config: tuple[str, ...] = ()
    note: str | None = None
    capabilities: dict[Operation, CapabilityInfo] = field(default_factory=dict)


def build_runtimes(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    aws_client_factory: ClientFactory | None = None,
) -> dict[str, ProviderRuntime]:
    http = {
        "timeout": settings.http_timeout_seconds,
        "transport": transport,
        "sleep": sleep,
    }
    runtimes: dict[str, ProviderRuntime] = {}

    def add(slug: str, build, missing: list[str], note: str | None = None) -> None:  # type: ignore[no-untyped-def]
        caps = dict(ADAPTER_CLASSES[slug].capabilities)
        if slug in settings.disabled_providers:
            runtimes[slug] = ProviderRuntime(
                slug,
                None,
                SyncStatus.DISABLED,
                note="listed in DISABLED_PROVIDERS",
                capabilities=caps,
            )
        elif missing:
            logger.warning("provider not configured", extra={"provider": slug, "missing": missing})
            runtimes[slug] = ProviderRuntime(
                slug, None, SyncStatus.NOT_CONFIGURED, tuple(missing), note, caps
            )
        else:
            runtimes[slug] = ProviderRuntime(slug, build(), SyncStatus.OK, capabilities=caps)

    s = settings
    add(
        "openai",
        lambda: OpenAIAdapter(
            s.openai_admin_key.get_secret_value(),  # type: ignore[union-attr]
            max_retries=s.http_max_retries,
            **http,
        ),
        [] if s.openai_admin_key else ["OPENAI_ADMIN_KEY"],
    )
    add("sarvam", lambda: SarvamAdapter(**http), [])
    add(
        "deepgram",
        lambda: DeepgramAdapter(
            s.deepgram_api_key.get_secret_value(),  # type: ignore[union-attr]
            s.deepgram_project_id or "",
            max_retries=s.http_max_retries,
            **http,
        ),
        [
            name
            for name, value in (
                ("DEEPGRAM_API_KEY", s.deepgram_api_key),
                ("DEEPGRAM_PROJECT_ID", s.deepgram_project_id),
            )
            if not value
        ],
    )
    add(
        "elevenlabs",
        lambda: ElevenLabsAdapter(
            s.elevenlabs_api_key.get_secret_value(),  # type: ignore[union-attr]
            max_retries=s.http_max_retries,
            **http,
        ),
        [] if s.elevenlabs_api_key else ["ELEVENLABS_API_KEY"],
    )

    def build_aws() -> AwsAdapter:
        factory = aws_client_factory or default_client_factory(
            profile=s.aws_profile,
            access_key_id=s.aws_access_key_id.get_secret_value() if s.aws_access_key_id else None,
            secret_access_key=(
                s.aws_secret_access_key.get_secret_value() if s.aws_secret_access_key else None
            ),
            session_token=s.aws_session_token.get_secret_value() if s.aws_session_token else None,
            timeout=s.http_timeout_seconds,
        )
        return AwsAdapter(
            factory,
            regions=s.aws_regions,
            cost_explorer_enabled=s.aws_cost_explorer_enabled,
            credits_enabled=s.aws_credits_enabled,
            resources_enabled=s.aws_resources_enabled,
        )

    if not s.aws_monitoring_enabled and "aws" not in s.disabled_providers:
        runtimes["aws"] = ProviderRuntime(
            "aws",
            None,
            SyncStatus.DISABLED,
            note="set AWS_MONITORING_ENABLED=true to enable (Cost Explorer costs $0.01/request)",
            capabilities=dict(AwsAdapter.capabilities),
        )
    else:
        try:
            add("aws", build_aws, [])
        except Exception as exc:  # e.g. AWS_PROFILE names a profile that doesn't exist
            logger.warning(
                "aws adapter could not be created", extra={"error_type": type(exc).__name__}
            )
            runtimes["aws"] = ProviderRuntime(
                "aws",
                None,
                SyncStatus.NOT_CONFIGURED,
                ("AWS_PROFILE or AWS credentials",),
                note=f"could not create AWS session ({type(exc).__name__})",
                capabilities=dict(AwsAdapter.capabilities),
            )
    return runtimes
