"""Configuration management for BetterSupport."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from dataclasses import dataclass, field


@dataclass
class SlackConfig:
    signing_secret: str
    bot_token: str
    response_mode: str = "thread"
    post_interim_message: bool = True
    ignore_bot_messages: bool = True
    ignore_edits_and_deletes: bool = True


@dataclass
class LLMConfig:
    provider: str
    api_key: str
    model: str = "gpt-4-turbo"
    max_tokens: int = 4096


@dataclass
class BudgetsConfig:
    max_tool_calls: int = 200
    max_subagent_calls: int = 3
    max_wall_clock_ms: int = 120000
    max_tokens: int = 300000
    max_search_results: int = 100
    max_file_bytes: int = 262144
    max_file_lines: int = 1200


@dataclass
class AccessConfig:
    deny_dir_names: List[str] = field(default_factory=list)
    deny_file_patterns: List[str] = field(default_factory=list)
    code_extensions: List[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    name: str
    repo_url: str
    branch: str = "main"
    agent_dir: str = ".github/agents"
    default_agent: str = "ProductLens"
    github_web_base_url: str = ""
    submodules: bool = False
    github_token: Optional[str] = None


@dataclass
class ChannelConfig:
    # Optional: a guruDecline-only channel never touches process_question(), so
    # it has no codebase project to search and doesn't need one configured.
    project: str = ""
    agent: str = "ProductLens"
    mode: str = "auto"
    triggers: Dict[str, bool] = field(default_factory=dict)
    # Slack user ID to DM when the guruDecline trigger fires. Required for any
    # channel with guruDecline enabled -- there is no auto-post path (by design).
    notify_user_id: Optional[str] = None


@dataclass
class BetterBrainConfig:
    repo_path: Path
    cli_timeout_seconds: int = 150


@dataclass
class Config:
    slack: SlackConfig
    llm: LLMConfig
    budgets: BudgetsConfig
    access: AccessConfig
    projects: Dict[str, ProjectConfig]
    channels: Dict[str, ChannelConfig]
    projects_dir: Path
    betterbrain: Optional[BetterBrainConfig] = None


def load_config(config_path: str = "config.yaml") -> Config:
    """Load configuration from YAML file and environment variables."""
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_file, "r") as f:
        data = yaml.safe_load(f)
    
    # Load Slack config
    slack_data = data.get("slack", {})
    signing_secret = os.getenv(slack_data.get("signingSecretEnv", "SLACK_SIGNING_SECRET"))
    bot_token = os.getenv(slack_data.get("botTokenEnv", "BETTERSUPPORT_SLACK_BOT_TOKEN"))
    
    if not signing_secret:
        raise ValueError("SLACK_SIGNING_SECRET not set")
    if not bot_token:
        raise ValueError("BETTERSUPPORT_SLACK_BOT_TOKEN not set")
    
    slack = SlackConfig(
        signing_secret=signing_secret,
        bot_token=bot_token,
        response_mode=slack_data.get("responseMode", "thread"),
        post_interim_message=slack_data.get("postInterimMessage", True),
        ignore_bot_messages=slack_data.get("ignoreBotMessages", True),
        ignore_edits_and_deletes=slack_data.get("ignoreEditsAndDeletes", True),
    )
    
    # Load LLM config
    llm_data = data.get("llm", {})
    api_key = os.getenv(llm_data.get("apiKeyEnv", "BETTERSUPPORT_LLM_OPENAI_API_KEY"))
    
    if not api_key:
        raise ValueError("BETTERSUPPORT_LLM_OPENAI_API_KEY not set")
    
    llm = LLMConfig(
        provider=llm_data.get("provider", "openai"),
        api_key=api_key,
        model=llm_data.get("model", "gpt-4-turbo"),
        max_tokens=llm_data.get("maxTokens", 4096),
    )
    
    # Load budgets
    budgets_data = data.get("budgets", {})
    budgets = BudgetsConfig(
        max_tool_calls=budgets_data.get("maxToolCalls", 200),
        max_subagent_calls=budgets_data.get("maxSubagentCalls", 3),
        max_wall_clock_ms=budgets_data.get("maxWallClockMs", 120000),
        max_tokens=budgets_data.get("maxTokens", 300000),
        max_search_results=budgets_data.get("maxSearchResults", 100),
        max_file_bytes=budgets_data.get("maxFileBytes", 262144),
        max_file_lines=budgets_data.get("maxFileLines", 1200),
    )
    
    # Load access config
    access_data = data.get("access", {})
    access = AccessConfig(
        deny_dir_names=access_data.get("denyDirNames", []),
        deny_file_patterns=access_data.get("denyFilePatterns", []),
        code_extensions=access_data.get("codeExtensions", []),
    )
    
    # Load projects
    projects_data = data.get("projects", {})
    projects = {}
    for name, proj_data in projects_data.items():
        github_token_env = proj_data.get("githubTokenEnv")
        github_token = os.getenv(github_token_env) if github_token_env else None
        
        projects[name] = ProjectConfig(
            name=name,
            repo_url=proj_data["repoUrl"],
            branch=proj_data.get("branch", "main"),
            agent_dir=proj_data.get("agentDir", ".github/agents"),
            default_agent=proj_data.get("defaultAgent", "ProductLens"),
            github_web_base_url=proj_data.get("githubWebBaseUrl", ""),
            submodules=proj_data.get("submodules", False),
            github_token=github_token,
        )
    
    # Load channels
    channels_data = data.get("channels", {})
    channels = {}
    for channel_id, chan_data in channels_data.items():
        channels[channel_id] = ChannelConfig(
            project=chan_data.get("project", ""),
            agent=chan_data.get("agent", "ProductLens"),
            mode=chan_data.get("mode", "auto"),
            triggers=chan_data.get("triggers", {}),
            notify_user_id=chan_data.get("notifyUserId"),
        )

    # Load BetterBrain config (only needed for channels using the guruDecline trigger)
    betterbrain_data = data.get("betterbrain")
    betterbrain = None
    if betterbrain_data:
        betterbrain = BetterBrainConfig(
            repo_path=Path(betterbrain_data["repoPath"]).expanduser(),
            cli_timeout_seconds=betterbrain_data.get("cliTimeoutSeconds", 150),
        )

    # Projects directory
    projects_dir = Path(__file__).parent.parent / "projects"
    projects_dir.mkdir(exist_ok=True)

    return Config(
        slack=slack,
        llm=llm,
        budgets=budgets,
        access=access,
        projects=projects,
        channels=channels,
        projects_dir=projects_dir,
        betterbrain=betterbrain,
    )
