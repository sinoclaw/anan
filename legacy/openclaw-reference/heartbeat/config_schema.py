"""
Config schema for heartbeat plugin.

Defines the configuration structure for the heartbeat plugin,
including per-agent settings and global defaults.
"""

from typing import Any, Dict, List, Optional

# Default heartbeat config schema
HEARTBEAT_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "default": True,
            "description": "Enable/disable the heartbeat plugin"
        },
        "interval_ms": {
            "type": "integer",
            "default": 1800000,  # 30 minutes
            "description": "Heartbeat interval in milliseconds"
        },
        "scheduler_seed": {
            "type": "string",
            "default": "anan-heartbeat-v1",
            "description": "Seed for phase-offset calculation (change to reset phase distribution)"
        },
        "flood_window_ms": {
            "type": "integer",
            "default": 60000,
            "description": "Window for flood guard (ms)"
        },
        "flood_threshold": {
            "type": "integer",
            "default": 5,
            "description": "Max heartbeats in window before flood guard triggers"
        },
        "min_spacing_ms": {
            "type": "integer",
            "default": 30000,
            "description": "Minimum spacing between heartbeat runs (ms)"
        },
        "ack_max_chars": {
            "type": "integer",
            "default": 300,
            "description": "Max chars after HEARTBEAT_OK before delivery"
        },
        "active_hours": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "example": "09:00"},
                "end": {"type": "string", "example": "22:00"},
                "timezone": {"type": "string", "example": "Asia/Shanghai"}
            },
            "description": "Restrict heartbeats to active hours window"
        },
        "state_file": {
            "type": "string",
            "default": "~/.anan/heartbeat-state.json",
            "description": "Path to persistent state file"
        },
        "agents": {
            "type": "object",
            "description": "Per-agent heartbeat configuration",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean", "default": True},
                    "interval_ms": {
                        "type": "integer",
                        "description": "Override global interval for this agent"
                    },
                    "prompt": {
                        "type": "string",
                        "default": "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. If nothing needs attention, reply HEARTBEAT_OK.",
                        "description": "Heartbeat prompt for this agent"
                    },
                    "activeHours": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "timezone": {"type": "string"}
                        }
                    },
                    "target": {
                        "type": "string",
                        "enum": ["last", "none"],
                        "default": "last",
                        "description": "Where to deliver heartbeat results"
                    },
                    "skipWhenBusy": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip heartbeat when subagent/nested lanes are busy"
                    },
                    "lightContext": {
                        "type": "boolean",
                        "default": False,
                        "description": "Use lightweight bootstrap (only HEARTBEAT.md)"
                    },
                    "isolatedSession": {
                        "type": "boolean",
                        "default": False,
                        "description": "Run heartbeat in fresh session (no history)"
                    },
                    "model": {
                        "type": "string",
                        "description": "Model override for heartbeat runs"
                    }
                }
            }
        }
    }
}


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize heartbeat config."""
    result = {}
    
    # Global settings
    result["enabled"] = config.get("enabled", True)
    result["interval_ms"] = config.get("interval_ms", 1800000)
    result["scheduler_seed"] = config.get("scheduler_seed", "anan-heartbeat-v1")
    result["flood_window_ms"] = config.get("flood_window_ms", 60000)
    result["flood_threshold"] = config.get("flood_threshold", 5)
    result["min_spacing_ms"] = config.get("min_spacing_ms", 30000)
    result["ack_max_chars"] = config.get("ack_max_chars", 300)
    result["state_file"] = config.get("state_file", "~/.anan/heartbeat-state.json")
    
    # Active hours
    if "active_hours" in config:
        ah = config["active_hours"]
        result["active_hours"] = {
            "start": ah.get("start", "00:00"),
            "end": ah.get("end", "24:00"),
            "timezone": ah.get("timezone", "Asia/Shanghai")
        }
    else:
        result["active_hours"] = None
    
    # Agent configs
    result["agents"] = {}
    for agent_id, agent_config in config.get("agents", {}).items():
        ac = {}
        ac["enabled"] = agent_config.get("enabled", True)
        
        if "interval_ms" in agent_config:
            ac["interval_ms"] = agent_config["interval_ms"]
        else:
            ac["interval_ms"] = result["interval_ms"]
        
        ac["prompt"] = agent_config.get(
            "prompt",
            "Read HEARTBEAT.md if it exists (workspace context). "
            "Follow it strictly. If nothing needs attention, reply HEARTBEAT_OK."
        )
        
        if "activeHours" in agent_config:
            ac["activeHours"] = agent_config["activeHours"]
        elif result["active_hours"]:
            ac["activeHours"] = result["active_hours"]
        else:
            ac["activeHours"] = None
        
        ac["target"] = agent_config.get("target", "last")
        ac["skipWhenBusy"] = agent_config.get("skipWhenBusy", False)
        ac["lightContext"] = agent_config.get("lightContext", False)
        ac["isolatedSession"] = agent_config.get("isolatedSession", False)
        ac["model"] = agent_config.get("model")
        
        result["agents"][agent_id] = ac
    
    return result


# Example config for documentation
EXAMPLE_CONFIG = """
# Sinoclaw config.yaml addition for heartbeat plugin

plugins:
  heartbeat:
    enabled: true
    interval_ms: 1800000  # 30 minutes
    scheduler_seed: "anan-heartbeat-v1"
    flood_window_ms: 60000
    flood_threshold: 5
    min_spacing_ms: 30000
    active_hours:
      start: "09:00"
      end: "22:00"
      timezone: "Asia/Shanghai"
    state_file: "~/.anan/heartbeat-state.json"
    
    agents:
      main:
        enabled: true
        interval_ms: 1800000  # 30 minutes
        prompt: |
          Read HEARTBEAT.md if it exists (workspace context).
          Follow it strictly. If nothing needs attention, reply HEARTBEAT_OK.
        target: "last"  # Deliver to last contact channel
        activeHours:
          start: "09:00"
          end: "22:00"
          timezone: "Asia/Shanghai"
        lightContext: true  # Only inject HEARTBEAT.md, not full history
        isolatedSession: false
      
      ops:
        enabled: true
        interval_ms: 3600000  # 1 hour
        prompt: |
          Check monitoring systems and report anomalies.
          If nothing needs attention, reply HEARTBEAT_OK.
        target: "last"
        skipWhenBusy: true
"""


if __name__ == "__main__":
    import json
    print("Heartbeat Plugin Config Schema")
    print("=" * 50)
    print(json.dumps(HEARTBEAT_CONFIG_SCHEMA, indent=2))
    print()
    print("Example Config:")
    print(EXAMPLE_CONFIG)