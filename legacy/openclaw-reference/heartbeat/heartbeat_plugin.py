"""
Heartbeat Plugin for Sinoclaw Gateway

Replicates OpenClaw's heartbeat mechanism:
- Phase-offset scheduling for multi-agent (SHA256-based)
- Active hours support (免打扰时段)
- Flood guard (60s内超过5次就跳过)
- Defer when cron is running
- HEARTBEAT_OK stripping and ackMaxChars
- Wake queue with coalescing and priority
- Duration parsing ("30m" -> ms)
- Per-agent enable/disable
- Delivery target resolution (last/none/channel)
- Typing indicators during heartbeat runs

Usage:
    from heartbeat_plugin import HeartbeatPlugin, register
    plugin = HeartbeatPlugin(config)
    await plugin.start()
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default config values (match OpenClaw defaults)
DEFAULT_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes
DEFAULT_ACK_MAX_CHARS = 300
DEFAULT_FLOOD_WINDOW_MS = 60 * 1000  # 60 seconds
DEFAULT_FLOOD_THRESHOLD = 5
DEFAULT_MIN_SPACING_MS = 30 * 1000  # 30 seconds
MAX_SAFE_TIMEOUT_DELAY_MS = 2147483647  # Node setTimeout cap

# Wake coalescing
DEFAULT_COALESCE_MS = 250
DEFAULT_RETRY_MS = 1000

# Heartbeat token
HEARTBEAT_TOKEN = "HEARTBEAT_OK"


def sha256_phase(agent_id: str, scheduler_seed: str, interval_ms: int) -> int:
    """Compute phase offset for an agent using SHA256 hash.
    
    This ensures multiple agents have dispersed heartbeat times
    rather than all firing simultaneously.
    
    OpenClaw uses: createHash("sha256").update(`${schedulerSeed}:${agentId}`).digest().readUInt32BE(0) % intervalMs
    """
    h = hashlib.sha256(f"{scheduler_seed}:{agent_id}".encode()).digest()
    phase = int.from_bytes(h[:4], byteorder="big") % interval_ms
    return phase


def normalize_modulo(value: int, divisor: int) -> int:
    """Normalize value to positive modulo range."""
    return (value % divisor + divisor) % divisor


def resolve_safe_timeout_delay_ms(raw_delay_ms: float) -> float:
    """Clamp delay to Node's setTimeout max (~24.85 days)."""
    if raw_delay_ms > MAX_SAFE_TIMEOUT_DELAY_MS:
        return MAX_SAFE_TIMEOUT_DELAY_MS
    return max(0, raw_delay_ms)


def parse_duration_ms(raw: str, default_unit: str = "m") -> int:
    """Parse duration string to milliseconds.
    
    Supports formats like:
    - "30m" -> 1800000
    - "1h" -> 3600000
    - "60s" -> 60000
    - "1d" -> 86400000
    
    OpenClaw's parseDurationMs implementation.
    """
    if not raw:
        return 0
    
    raw = str(raw).strip().lower()
    
    # Try parsing as plain number (assume milliseconds)
    try:
        val = float(raw)
        if val <= 0:
            return 0
        return int(val)
    except ValueError:
        pass
    
    # Parse with unit
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd])?$", raw)
    if not match:
        return 0
    
    value = float(match.group(1))
    unit = match.group(2) or default_unit
    
    multipliers = {
        "s": 1000,       # seconds
        "m": 60 * 1000, # minutes
        "h": 60 * 60 * 1000,  # hours
        "d": 24 * 60 * 60 * 1000,  # days
    }
    
    multiplier = multipliers.get(unit, 60 * 1000)  # default to minutes
    
    result = int(value * multiplier)
    return result if result > 0 else 0


def parse_active_hours_time(raw: str, allow_24: bool = False) -> Optional[int]:
    """Parse HH:MM time string to minutes since midnight.
    
    Returns None if malformed.
    allow_24: Whether to accept 24:00 (converted to 1440).
    """
    if not raw:
        return None
    pattern = r"^(?:([01]\d|2[0-3]):([0-5]\d)|24:00)$"
    if not re.match(pattern, raw):
        return None
    parts = raw.split(":")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour == 24:
        if not allow_24 or minute != 0:
            return None
        return 1440
    return hour * 60 + minute


def get_minutes_in_timezone(tz: str) -> Optional[int]:
    """Get current minutes since midnight in the given timezone."""
    try:
        import pytz
        from datetime import timezone
        now_utc = datetime.now(timezone.utc)
        local_tz = pytz.timezone(tz)
        local_dt = now_utc.astimezone(local_tz)
        hour = int(local_dt.strftime("%H"))
        minute = int(local_dt.strftime("%M"))
        return hour * 60 + minute
    except Exception:
        # Fallback to local time
        now = datetime.now()
        return now.hour * 60 + now.minute


# ---------------------------------------------------------------------------
# HEARTBEAT_TOKEN Stripping
# ---------------------------------------------------------------------------

def strip_token_at_edges(text: str) -> Tuple[str, bool]:
    """Strip HEARTBEAT_OK from start/end of text.
    
    Returns (stripped_text, did_strip).
    """
    if not text:
        return "", False
    
    token = HEARTBEAT_TOKEN
    
    # Token at end with optional trailing punctuation (up to 4 chars)
    end_pattern = re.compile(f"{re.escape(token)}[^\\w]{{0,4}}$")
    
    did_strip = False
    
    # Strip from start
    while text.startswith(token):
        text = text[len(token):].lstrip()
        did_strip = True
    
    # Strip from end (with optional trailing punctuation)
    match = end_pattern.search(text)
    while match:
        idx = match.start()
        before = text[:idx].rstrip()
        if not before:
            text = ""
        else:
            text = before
        did_strip = True
        match = end_pattern.search(text)
    
    return text.strip(), did_strip


def strip_heartbeat_token(
    raw: str,
    mode: str = "message",
    max_ack_chars: int = DEFAULT_ACK_MAX_CHARS
) -> Tuple[bool, str, bool]:
    """Process heartbeat response, strip HEARTBEAT_OK if present.
    
    Returns (should_skip, text, did_strip).
    
    Mode "heartbeat": HEARTBEAT_OK at edges + ≤maxAckChars remaining = skip
    Mode "message": Only strip if explicit HEARTBEAT_OK found
    """
    if not raw:
        return True, "", False
    
    trimmed = raw.strip()
    if not trimmed:
        return True, "", False
    
    # Normalize (remove markup)
    def strip_markup(text: str) -> str:
        text = re.sub(r"<[^>]*>", " ", text)
        text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"^[*`~_]+", "", text)
        text = re.sub(r"[*`~_]+$", "", text)
        return text
    
    trimmed_normalized = strip_markup(trimmed)
    
    # Check if contains HEARTBEAT_OK (original or normalized)
    has_token = (
        HEARTBEAT_TOKEN in trimmed or 
        HEARTBEAT_TOKEN in trimmed_normalized
    )
    
    if not has_token:
        return False, trimmed, False
    
    # Strip token at edges
    stripped_original, did_strip_original = strip_token_at_edges(trimmed)
    stripped_normalized, did_strip_normalized = strip_token_at_edges(trimmed_normalized)
    
    # Pick the one that was stripped and has content
    picked_text = ""
    did_strip = False
    
    if did_strip_original and stripped_original:
        picked_text = stripped_original
        did_strip = True
    elif did_strip_normalized and stripped_normalized:
        picked_text = stripped_normalized
        did_strip = True
    
    if not did_strip:
        return False, trimmed, False
    
    rest = picked_text.strip()
    
    if mode == "heartbeat":
        # In heartbeat mode: token at edges + short content = skip
        if not rest or len(rest) <= max_ack_chars:
            return True, "", True
    
    return False, rest, True


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PendingWake:
    """A pending heartbeat wake request."""
    source: str
    intent: str  # "manual" | "immediate" | "scheduled" | "event"
    reason: str
    priority: int
    requested_at: float
    agent_id: Optional[str]
    session_key: Optional[str]
    heartbeat: Optional[Dict] = None


@dataclass
class HeartbeatAgent:
    """Per-agent heartbeat state."""
    agent_id: str
    interval_ms: int
    phase_ms: int
    next_due_ms: float
    last_run_started_at_ms: Optional[float] = None
    recent_run_starts: List[float] = field(default_factory=list)
    flood_logged_since_last_run: bool = False
    heartbeat_config: Dict[str, Any] = field(default_factory=dict)
    active_hours_schedule: Optional[Dict] = None


@dataclass
class HeartbeatConfig:
    """Heartbeat plugin configuration."""
    enabled: bool = True
    interval_ms: int = DEFAULT_INTERVAL_MS
    scheduler_seed: str = "anan-heartbeat-v1"
    flood_window_ms: int = DEFAULT_FLOOD_WINDOW_MS
    flood_threshold: int = DEFAULT_FLOOD_THRESHOLD
    min_spacing_ms: int = DEFAULT_MIN_SPACING_MS
    ack_max_chars: int = DEFAULT_ACK_MAX_CHARS
    active_hours: Optional[Dict[str, str]] = None
    state_file: str = "~/.anan/heartbeat-state.json"
    
    # Per-agent configs
    agent_configs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    # Delivery settings
    target: str = "last"
    show_ok: bool = False
    show_alerts: bool = True
    use_indicator: bool = True
    
    # Wake coalescing
    coalesce_ms: int = DEFAULT_COALESCE_MS
    retry_ms: int = DEFAULT_RETRY_MS


class HeartbeatState:
    """Persistent state for heartbeat plugin."""
    
    def __init__(self, state_file: str):
        self.state_file = Path(state_file).expanduser()
        self.data: Dict[str, Any] = {}
        self.load()
    
    def load(self) -> None:
        """Load state from disk."""
        if self.state_file.exists():
            try:
                import json
                self.data = json.loads(self.state_file.read_text())
            except Exception:
                self.data = {}
    
    def save(self) -> None:
        """Persist state to disk."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        import json
        self.state_file.write_text(json.dumps(self.data, indent=2))
    
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()
    
    def update_last_run(self, agent_id: str) -> None:
        """Update last run timestamp for an agent."""
        if "agent_last_run" not in self.data:
            self.data["agent_last_run"] = {}
        self.data["agent_last_run"][agent_id] = time.time()
        self.save()
    
    def get_last_run(self, agent_id: str) -> Optional[float]:
        """Get last run timestamp for an agent."""
        return self.data.get("agent_last_run", {}).get(agent_id)


# ---------------------------------------------------------------------------
# Heartbeat Plugin
# ---------------------------------------------------------------------------

class HeartbeatPlugin:
    """
    Heartbeat plugin for Sinoclaw.
    
    Replicates OpenClaw's heartbeat scheduling with:
    - Phase-offset multi-agent scheduling
    - Active hours (免打扰)
    - Flood guard
    - Defer when cron busy
    - HEARTBEAT_OK stripping and ackMaxChars
    - Wake queue with coalescing and priority
    - Per-agent enable/disable
    - Delivery routing (last/none/channel)
    """
    
    # Priority levels for wake coalescing
    PRIORITY_RETRY = 0
    PRIORITY_INTERVAL = 1
    PRIORITY_DEFAULT = 2
    PRIORITY_ACTION = 3
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = HeartbeatConfig(**(config or {}))
        self.state = HeartbeatState(self.config.state_file)
        self.agents: Dict[str, HeartbeatAgent] = {}
        self.running = False
        self._timer: Optional[asyncio.TimerHandle] = None
        self._timer_due_at: Optional[float] = None
        self._timer_kind: Optional[str] = None
        self._scheduler_seed = self.config.scheduler_seed
        
        # Pending wakes for coalescing
        self._pending_wakes: Dict[str, PendingWake] = {}
        
        # Hook handlers
        self._hook_handlers: Dict[str, List] = {
            "on_heartbeat_tick": [],
            "on_heartbeat_run": [],
            "on_heartbeat_skip": [],
            "on_heartbeat_delivery": [],
        }
        
        # Flags
        self._cron_busy = False
        self._requests_in_flight = 0
        
        # Last contact tracking (for target="last")
        self._last_contact: Dict[str, str] = {}
        
        # Global heartbeat enabled
        self._heartbeats_enabled = True
    
    def set_heartbeats_enabled(self, enabled: bool) -> None:
        """Enable/disable heartbeats globally."""
        self._heartbeats_enabled = enabled
    
    def are_heartbeats_enabled(self) -> bool:
        """Check if heartbeats are globally enabled."""
        return self._heartbeats_enabled
    
    def register_hook(self, hook_name: str, handler) -> None:
        """Register a hook handler."""
        if hook_name not in self._hook_handlers:
            self._hook_handlers[hook_name] = []
        self._hook_handlers[hook_name].append(handler)
    
    async def _emit_hook(self, hook_name: str, *args, **kwargs) -> None:
        """Emit a hook to all registered handlers."""
        for handler in self._hook_handlers.get(hook_name, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    handler(*args, **kwargs)
            except Exception as e:
                logger.debug(f"Hook {hook_name} error: {e}")
    
    def _resolve_wake_priority(self, source: str, intent: str, reason: str) -> int:
        """Resolve wake priority for coalescing."""
        if intent in ("manual", "immediate"):
            return self.PRIORITY_ACTION
        if source == "retry" or reason == "retry":
            return self.PRIORITY_RETRY
        if intent == "scheduled" or source == "interval" or reason == "interval":
            return self.PRIORITY_INTERVAL
        return self.PRIORITY_DEFAULT
    
    def _get_wake_target_key(self, agent_id: Optional[str], session_key: Optional[str]) -> str:
        """Get key for pending wake deduplication."""
        return f"{agent_id or ''}::{session_key or ''}"
    
    def request_heartbeat(
        self,
        source: str = "api",
        intent: str = "event",
        reason: str = "requested",
        agent_id: Optional[str] = None,
        session_key: Optional[str] = None,
        heartbeat: Optional[Dict] = None,
        coalesce_ms: Optional[float] = None
    ) -> None:
        """Request a heartbeat wake, with coalescing.
        
        OpenClaw's requestHeartbeat() - queues wake requests and schedules
        a tick with optional coalescing to batch multiple requests.
        """
        if not self._heartbeats_enabled:
            return
        
        if not self.agents:
            return
        
        requested_at = time.time() * 1000
        priority = self._resolve_wake_priority(source, intent, reason)
        
        wake_key = self._get_wake_target_key(agent_id, session_key)
        
        new_wake = PendingWake(
            source=source,
            intent=intent,
            reason=reason,
            priority=priority,
            requested_at=requested_at,
            agent_id=agent_id,
            session_key=session_key,
            heartbeat=heartbeat,
        )
        
        # Coalesce: keep higher priority or more recent
        existing = self._pending_wakes.get(wake_key)
        if existing:
            if new_wake.priority > existing.priority:
                self._pending_wakes[wake_key] = new_wake
            elif new_wake.priority == existing.priority and new_wake.requested_at >= existing.requested_at:
                self._pending_wakes[wake_key] = new_wake
            # Otherwise keep existing (lower priority or older)
        else:
            self._pending_wakes[wake_key] = new_wake
        
        # Schedule tick with coalescing
        coalesce = coalesce_ms if coalesce_ms is not None else self.config.coalesce_ms
        self._schedule_with_coalesce(coalesce)
    
    def _schedule_with_coalesce(self, coalesce_ms: float) -> None:
        """Schedule next tick with coalescing delay."""
        now = time.time() * 1000
        due_at = now + coalesce_ms
        
        # If we already have a timer scheduled earlier, don't reschedule
        if self._timer and self._timer_due_at and due_at >= self._timer_due_at:
            return
        
        # Cancel existing timer
        if self._timer:
            self._timer.cancel()
        
        delay_s = coalesce_ms / 1000
        
        loop = asyncio.get_event_loop()
        self._timer = loop.call_later(delay_s, lambda: asyncio.create_task(self._process_wakes()))
        self._timer_due_at = due_at
        self._timer_kind = "coalesced"
    
    async def _process_wakes(self) -> None:
        """Process pending wakes and run heartbeats."""
        if not self._pending_wakes:
            return
        
        self._pending_wakes.clear()
        
        now_ms = time.time() * 1000
        
        for agent in list(self.agents.values()):
            # Check deferral
            defer, reason = self._should_defer(agent, now_ms, "event", "wake")
            if defer:
                await self._emit_hook("on_heartbeat_skip", agent.agent_id, reason)
                continue
            
            # Run heartbeat
            await self._run_heartbeat(agent, now_ms, "event")
    
    def is_heartbeat_enabled_for_agent(self, agent_id: str) -> bool:
        """Check if heartbeat is enabled for a specific agent."""
        if agent_id in self.agents:
            return True
        
        # Check if defaults enable it
        if self.config.agent_configs:
            # Has explicit config
            return agent_id in self.config.agent_configs
        
        # Check if global defaults enable it
        return bool(self.config.enabled)
    
    def load_agents(self, agent_configs: Dict[str, Dict[str, Any]]) -> None:
        """Load/update agent configurations."""
        now_ms = time.time() * 1000
        
        for agent_id, agent_config in agent_configs.items():
            # Check if agent has heartbeat explicitly disabled
            if agent_config.get("enabled") is False:
                if agent_id in self.agents:
                    del self.agents[agent_id]
                continue
            
            interval_raw = agent_config.get("every") or agent_config.get("interval_ms")
            if interval_raw:
                if isinstance(interval_raw, str):
                    interval_ms = parse_duration_ms(interval_raw, default_unit="m")
                else:
                    interval_ms = int(interval_raw)
            else:
                interval_ms = self.config.interval_ms
            
            if interval_ms <= 0:
                continue
            
            # Get or create agent state
            if agent_id in self.agents:
                agent = self.agents[agent_id]
                agent.interval_ms = interval_ms
                agent.heartbeat_config = agent_config
            else:
                # Compute phase offset for new agent
                phase_ms = sha256_phase(agent_id, self._scheduler_seed, interval_ms)
                
                agent = HeartbeatAgent(
                    agent_id=agent_id,
                    interval_ms=interval_ms,
                    phase_ms=phase_ms,
                    next_due_ms=0,
                    heartbeat_config=agent_config,
                )
                self.agents[agent_id] = agent
            
            # Resolve active hours for this agent
            if agent_config.get("activeHours"):
                agent.active_hours_schedule = agent_config["activeHours"]
            elif self.config.active_hours:
                agent.active_hours_schedule = self.config.active_hours
            else:
                agent.active_hours_schedule = None
            
            # Compute initial next due (seek first active slot)
            agent.next_due_ms = self._seek_next_active_phase_due_ms(
                now_ms, interval_ms, agent.phase_ms, agent
            )
    
    def _is_within_active_hours(self, agent: HeartbeatAgent, now_ms: float) -> bool:
        """Check if current time is within active hours for the agent."""
        ah = agent.active_hours_schedule or self.config.active_hours
        if not ah:
            return True
        
        start = parse_active_hours_time(ah.get("start", "00:00"))
        end = parse_active_hours_time(ah.get("end", "24:00"), allow_24=True)
        tz = ah.get("timezone", "Asia/Shanghai")
        
        if start is None or end is None:
            return True
        
        current_min = get_minutes_in_timezone(tz)
        if current_min is None:
            return True
        
        # Handle wrap-around (e.g., 22:00 to 08:00 means active from 22:00 to 24:00 OR 00:00 to 08:00)
        if end > start:
            in_range = start <= current_min < end
        else:
            in_range = current_min >= start or current_min < end
        
        return in_range
    
    def _seek_next_active_phase_due_ms(
        self,
        start_ms: float,
        interval_ms: int,
        phase_ms: int,
        agent: HeartbeatAgent
    ) -> float:
        """Find next time slot that falls within active hours.
        
        Uses phase offset from start to spread heartbeats across the interval.
        """
        # Phase-aligned candidate
        candidate = start_ms + phase_ms
        
        # If we've passed this slot, move to next interval
        if candidate <= start_ms:
            candidate += interval_ms
        
        # Check and advance until in active hours
        max_iterations = 100
        iterations = 0
        
        while not self._is_within_active_hours(agent, candidate) and iterations < max_iterations:
            candidate += interval_ms
            iterations += 1
        
        if iterations >= max_iterations:
            # Fallback: just add interval
            candidate = start_ms + interval_ms
        
        return candidate
    
    def _should_defer(
        self, 
        agent: HeartbeatAgent, 
        now_ms: float, 
        intent: str = "event",
        reason: str = ""
    ) -> Tuple[bool, str]:
        """Decide whether to defer a heartbeat run.
        
        Returns (defer, reason).
        
        Intent: "manual" | "immediate" | "scheduled" | "event"
        - manual: Never defer
        - immediate: Run now, check flood guard
        - scheduled: Defer if not due
        - event: Defer if not due or within floor
        """
        # Manual intent never deferred
        if intent == "manual":
            return False, ""
        
        # Immediate: only defer for flood guard
        if intent == "immediate":
            flood_defer = self._check_flood_guard(agent, now_ms)
            if flood_defer:
                return True, "flood"
            return False, ""
        
        # Requests in flight
        if self._requests_in_flight > 0:
            return True, "requests-in-flight"
        
        # Cron busy (always defers for non-manual)
        if self._cron_busy:
            return True, "cron-in-progress"
        
        # Check lanes busy (subagent/nested work)
        if agent.heartbeat_config.get("skipWhenBusy"):
            if self._is_lanes_busy():
                return True, "lanes-busy"
        
        # Scheduled/event: defer if not due
        if agent.next_due_ms and now_ms < agent.next_due_ms:
            return True, "not-due"
        
        # Min spacing check
        if agent.last_run_started_at_ms:
            elapsed = now_ms - agent.last_run_started_at_ms
            if elapsed < self.config.min_spacing_ms:
                return True, "min-spacing"
        
        # Flood guard
        flood_defer, flood_reason = self._check_flood_guard_with_logging(agent, now_ms, reason)
        if flood_defer:
            return True, flood_reason
        
        return False, ""
    
    def _is_lanes_busy(self) -> bool:
        """Check if subagent/nested lanes are busy."""
        # This would be connected to the lane system
        # For now, just check if we have requests in flight
        return self._requests_in_flight > 0
    
    def _check_flood_guard(self, agent: HeartbeatAgent, now_ms: float) -> bool:
        """Check flood guard without logging."""
        recent = agent.recent_run_starts
        flood_window = self.config.flood_window_ms
        flood_threshold = self.config.flood_threshold
        
        if len(recent) < flood_threshold:
            return False
        
        window_start = now_ms - flood_window
        in_window = sum(1 for ts in recent if ts >= window_start)
        return in_window >= flood_threshold
    
    def _check_flood_guard_with_logging(
        self, 
        agent: HeartbeatAgent, 
        now_ms: float, 
        reason: str = ""
    ) -> Tuple[bool, str]:
        """Check flood guard with warning log."""
        recent = agent.recent_run_starts
        flood_window = self.config.flood_window_ms
        flood_threshold = self.config.flood_threshold
        
        if len(recent) < flood_threshold:
            return False, ""
        
        window_start = now_ms - flood_window
        in_window = sum(1 for ts in recent if ts >= window_start)
        
        if in_window >= flood_threshold:
            if not agent.flood_logged_since_last_run:
                logger.warning(
                    f"heartbeat: flood guard tripped for {agent.agent_id}, "
                    f"deferring wake (reason={reason or 'none'}, recent_count={len(recent)})"
                )
                agent.flood_logged_since_last_run = True
            return True, "flood"
        
        return False, ""
    
    def _record_run_start(self, agent: HeartbeatAgent, now_ms: float) -> None:
        """Record a run start for flood guard and state."""
        agent.last_run_started_at_ms = now_ms
        agent.recent_run_starts.append(now_ms)
        
        # Trim to flood_threshold + 1 entries
        max_entries = self.config.flood_threshold + 1
        while len(agent.recent_run_starts) > max_entries:
            agent.recent_run_starts.pop(0)
        
        agent.flood_logged_since_last_run = False
        
        # Update persistent state
        self.state.update_last_run(agent.agent_id)
    
    def _advance_agent_schedule(
        self, 
        agent: HeartbeatAgent, 
        now_ms: float, 
        reason: str
    ) -> None:
        """Advance agent's schedule after a run."""
        if reason == "interval":
            agent.next_due_ms = self._seek_next_active_phase_due_ms(
                now_ms, agent.interval_ms, agent.phase_ms, agent
            )
        else:
            # For non-interval reasons (manual, event), schedule next interval from now
            agent.next_due_ms = now_ms + agent.interval_ms
    
    def _schedule_next(self) -> None:
        """Schedule the next periodic heartbeat tick."""
        if self._timer:
            self._timer.cancel()
            self._timer = None
        
        if not self.agents:
            return
        
        now_ms = time.time() * 1000
        next_due = min(agent.next_due_ms for agent in self.agents.values())
        
        delay_ms = max(0, next_due - now_ms)
        delay_s = resolve_safe_timeout_delay_ms(delay_ms) / 1000
        
        logger.debug(f"heartbeat: next periodic tick in {delay_s:.1f}s")
        
        loop = asyncio.get_event_loop()
        self._timer = loop.call_later(delay_s, lambda: asyncio.create_task(self._on_tick()))
        self._timer_due_at = now_ms + delay_ms
        self._timer_kind = "interval"
    
    async def _on_tick(self) -> None:
        """Handle a periodic heartbeat tick."""
        now_ms = time.time() * 1000
        
        for agent in list(self.agents.values()):
            # Skip if not yet due
            if now_ms < agent.next_due_ms:
                continue
            
            # Check deferral
            defer, reason = self._should_defer(agent, now_ms, "scheduled", "interval")
            if defer:
                await self._emit_hook("on_heartbeat_skip", agent.agent_id, reason)
                self._advance_agent_schedule(agent, now_ms, "interval")
                continue
            
            # Run heartbeat
            await self._run_heartbeat(agent, now_ms, "interval")
        
        # Schedule next tick
        self._schedule_next()
    
    async def _run_heartbeat(
        self,
        agent: HeartbeatAgent,
        now_ms: float,
        reason: str
    ) -> None:
        """Execute a heartbeat for an agent."""
        logger.info(f"heartbeat: running for {agent.agent_id} (reason={reason})")
        
        # Record bookkeeping
        self._record_run_start(agent, now_ms)
        
        # Emit run started hook
        await self._emit_hook("on_heartbeat_run", agent.agent_id)
        
        # Build heartbeat prompt
        default_prompt = (
            "Read HEARTBEAT.md if it exists (workspace context). "
            "Follow it strictly. If nothing needs attention, reply HEARTBEAT_OK."
        )
        prompt = agent.heartbeat_config.get("prompt") or default_prompt
        
        # Emit tick event (will be caught for session delivery)
        event = {
            "agent_id": agent.agent_id,
            "prompt": prompt,
            "intent": "scheduled" if reason == "interval" else reason,
            "reason": reason,
            "timestamp": now_ms,
            "target": agent.heartbeat_config.get("target", self.config.target),
        }
        
        await self._emit_hook("on_heartbeat_tick", event)
        
        # Advance schedule
        self._advance_agent_schedule(agent, now_ms, reason)
    
    async def trigger_heartbeat(
        self, 
        agent_id: str, 
        intent: str = "manual"
    ) -> bool:
        """Manually trigger a heartbeat for an agent.
        
        Args:
            agent_id: Target agent
            intent: "manual" (immediate, never defers) or "immediate" (flood-protected)
        
        Returns:
            True if heartbeat was triggered, False if deferred.
        """
        if agent_id not in self.agents:
            logger.warning(f"heartbeat: unknown agent {agent_id}")
            return False
        
        agent = self.agents[agent_id]
        now_ms = time.time() * 1000
        
        defer, reason = self._should_defer(agent, now_ms, intent, "manual")
        if defer:
            logger.info(f"heartbeat: deferring {intent} trigger for {agent_id} ({reason})")
            return False
        
        await self._run_heartbeat(agent, now_ms, intent)
        return True
    
    def set_last_contact(self, agent_id: str, channel_id: str) -> None:
        """Update last contact for target="last" resolution."""
        self._last_contact[agent_id] = channel_id
    
    def set_cron_busy(self, busy: bool) -> None:
        """Set cron busy flag (called by cron system)."""
        was_busy = self._cron_busy
        self._cron_busy = busy
        if was_busy and not busy:
            logger.debug("heartbeat: cron finished, heartbeat may resume")
    
    def increment_requests_in_flight(self) -> None:
        """Increment requests in flight counter."""
        self._requests_in_flight += 1
    
    def decrement_requests_in_flight(self) -> None:
        """Decrement requests in flight counter."""
        self._requests_in_flight = max(0, self._requests_in_flight - 1)
    
    def resolve_delivery_target(self, agent_id: str, target: str) -> Optional[str]:
        """Resolve delivery target (last/none/channel_id)."""
        if target == "none":
            return None
        if target == "last":
            return self._last_contact.get(agent_id)
        return target
    
    async def deliver_response(
        self,
        agent_id: str,
        response: str,
        metadata: Optional[Dict] = None
    ) -> None:
        """Deliver a heartbeat response to the appropriate channel.
        
        Handles HEARTBEAT_OK stripping and ackMaxChars logic.
        """
        # Process response
        should_skip, content, did_strip = strip_heartbeat_token(
            response,
            mode="heartbeat",
            max_ack_chars=self.config.ack_max_chars
        )
        
        if should_skip:
            logger.debug(f"heartbeat: response suppressed for {agent_id} (ack)")
            return
        
        if not content:
            return
        
        # Get delivery target
        target = self.resolve_delivery_target(
            agent_id,
            metadata.get("target", "last") if metadata else "last"
        )
        
        if not target:
            logger.debug(f"heartbeat: no delivery target for {agent_id}")
            return
        
        # Emit delivery hook
        await self._emit_hook("on_heartbeat_delivery", {
            "agent_id": agent_id,
            "content": content,
            "target": target,
            "metadata": metadata,
        })
    
    async def start(self) -> None:
        """Start the heartbeat scheduler."""
        self.running = True
        self._schedule_next()
        logger.info("heartbeat: plugin started")
    
    async def stop(self) -> None:
        """Stop the heartbeat scheduler."""
        self.running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info("heartbeat: plugin stopped")
    
    def get_status(self) -> Dict[str, Any]:
        """Get current heartbeat status."""
        now_ms = time.time() * 1000
        return {
            "running": self.running,
            "enabled": self._heartbeats_enabled,
            "cron_busy": self._cron_busy,
            "requests_in_flight": self._requests_in_flight,
            "pending_wakes": len(self._pending_wakes),
            "agents": {
                aid: {
                    "interval_ms": a.interval_ms,
                    "next_due_ms": a.next_due_ms,
                    "next_due_in_s": max(0, (a.next_due_ms - now_ms) / 1000),
                    "last_run_ms": a.last_run_started_at_ms,
                    "recent_runs": len(a.recent_run_starts),
                    "flood_logged": a.flood_logged_since_last_run,
                }
                for aid, a in self.agents.items()
            }
        }


# ---------------------------------------------------------------------------
# HEARTBEAT.md Helpers
# ---------------------------------------------------------------------------

def is_heartbeat_content_effectively_empty(content: Optional[str]) -> bool:
    """Check if HEARTBEAT.md content is effectively empty.
    
    Returns True if file has no actionable tasks.
    Returns False if file doesn't exist (so LLM can decide).
    
    A file is considered effectively empty if it contains only:
    - Whitespace / empty lines
    - Markdown ATX headers (`#`, `##`, ...)
    - Markdown fence markers such as ``` or ```markdown
    - Empty list item stubs (`- `, `- [ ]`, `* `, `+ `)
    """
    if content is None or content == "":
        return False
    
    lines = content.split("\n")
    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue
        # Skip markdown headers
        if re.match(r"^#+\s", trimmed):
            continue
        # Skip empty list items
        if re.match(r"^[-*+]\s*(\[[\sXx]?\]\s*)?$", trimmed):
            continue
        # Skip code fences
        if re.match(r"^```[A-Za-z0-9_-]*$", trimmed):
            continue
        return False
    return True


def parse_heartbeat_tasks(content: str) -> List[Dict[str, Any]]:
    """Parse heartbeat tasks from HEARTBEAT.md content.
    
    Supports YAML-like task definitions:
    
    tasks:
      - name: email-check
        interval: 30m
        prompt: "Check for urgent unread emails"
    """
    tasks = []
    lines = content.split("\n")
    in_tasks_block = False
    
    for i, line in enumerate(lines):
        trimmed = line.strip()
        
        if trimmed == "tasks:":
            in_tasks_block = True
            continue
        
        if not in_tasks_block:
            continue
        
        # Check if we've left the tasks block
        if trimmed and not trimmed.startswith(" ") and not trimmed.startswith("\t"):
            if not trimmed.startswith("-") and not trimmed.startswith("interval:") and not trimmed.startswith("prompt:"):
                in_tasks_block = False
                continue
        
        if trimmed.startswith("- name:"):
            name = trimmed.replace("- name:", "").strip().strip("\"'")
            
            # Look for interval and prompt in subsequent lines
            interval = ""
            prompt = ""
            
            for j in range(i + 1, len(lines)):
                next_line = lines[j]
                next_trimmed = next_line.strip()
                
                # Stop if we hit another task
                if next_trimmed.startswith("- name:"):
                    break
                
                # Skip if not indented
                if next_line and not next_line.startswith(" ") and not next_line.startswith("\t"):
                    if next_trimmed:
                        in_tasks_block = False
                        break
                
                if next_trimmed.startswith("interval:"):
                    interval = next_trimmed.replace("interval:", "").strip().strip("\"'")
                elif next_trimmed.startswith("prompt:"):
                    prompt = next_trimmed.replace("prompt:", "").strip().strip("\"'")
            
            if name and interval and prompt:
                tasks.append({
                    "name": name,
                    "interval": interval,
                    "prompt": prompt,
                })
    
    return tasks


def is_task_due(task: Dict[str, Any], last_run_ts: Optional[float]) -> bool:
    """Check if a heartbeat task is due.
    
    Args:
        task: Task definition with 'interval' key
        last_run_ts: Last run timestamp
    
    Returns:
        True if task should run now.
    """
    if not last_run_ts:
        return True
    
    interval = task.get("interval", "30m")
    interval_ms = parse_duration_ms(interval, default_unit="m")
    
    now = time.time() * 1000
    return (now - last_run_ts * 1000) >= interval_ms


# ---------------------------------------------------------------------------
# Plugin Registration
# ---------------------------------------------------------------------------

_plugin_instance: Optional[HeartbeatPlugin] = None


def get_plugin() -> Optional[HeartbeatPlugin]:
    """Get the plugin instance."""
    return _plugin_instance


def set_plugin(plugin: HeartbeatPlugin) -> None:
    """Set the plugin instance."""
    global _plugin_instance
    _plugin_instance = plugin


def register(ctx) -> None:
    """Register the heartbeat plugin with the gateway.
    
    Args:
        ctx: PluginContext from Sinoclaw gateway
    """
    global _plugin_instance
    
    config = ctx.get_config("heartbeat", {})
    
    plugin = HeartbeatPlugin(config)
    _plugin_instance = plugin
    
    # Register hooks
    async def on_cron_job_start():
        plugin.set_cron_busy(True)
    
    async def on_cron_job_end():
        plugin.set_cron_busy(False)
    
    ctx.register_hook("pre_cron_job", on_cron_job_start)
    ctx.register_hook("post_cron_job", on_cron_job_end)
    
    # Heartbeat tick hook - sends message to main session
    async def on_heartbeat_tick(event):
        # Build message for main session
        session_key = f"agent:{event['agent_id']}:main"
        
        # Send to session via gateway
        await ctx.send_to_session(
            session_key=session_key,
            message=event["prompt"],
            metadata={
                "heartbeat": True,
                "source": "heartbeat-plugin",
                "intent": event.get("intent"),
                "reason": event.get("reason"),
            }
        )
    
    ctx.register_hook("on_heartbeat_tick", on_heartbeat_tick)
    
    # Heartbeat delivery hook - send response to channel
    async def on_heartbeat_delivery(delivery):
        channel_id = delivery.get("target")
        content = delivery.get("content")
        
        if channel_id and content:
            await ctx.send_to_channel(
                channel_id=channel_id,
                content=content,
                metadata={"heartbeat_response": True}
            )
    
    ctx.register_hook("on_heartbeat_delivery", on_heartbeat_delivery)
    
    # Load agent configs
    agent_configs = config.get("agents", {})
    if not agent_configs and config.get("enabled", True):
        # Use default agent if enabled and no explicit configs
        agent_configs = {"main": {}}
    
    if agent_configs:
        plugin.load_agents(agent_configs)
    
    # Start plugin
    asyncio.create_task(plugin.start())
    
    logger.info("heartbeat: plugin registered")