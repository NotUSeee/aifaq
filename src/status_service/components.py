"""What this page reports, in the words a customer would use.

Two layers:

* ``SERVICES`` — every check we store, keyed by the name the platform (or one
  of our own probes) reports it under. These are the "platform components"
  the main site promises: dashboard, gateway, database, cache and so on.
* ``GROUPS`` — the customer-facing components. Each one rolls up the checks
  behind a thing a server owner actually uses ("Custom bots"), so the page
  answers "is my stuff working" before it answers "which process is down".

Nothing here measures anything. Probes write rows under a service name; this
module only decides how those rows are named, described and grouped.
"""

from __future__ import annotations

from dataclasses import dataclass

# How a check is taken. Shown next to each check so a reader can tell a
# measurement made from outside apart from one the platform reports itself.
OUTSIDE = "outside"   # our monitor, over the public internet
INSIDE = "inside"     # the platform's own health checker, read via /status/api
DISCORD = "discord"   # Discord's public status page

HOW_LABEL = {
    OUTSIDE: "Checked from outside",
    INSIDE: "Reported by the platform",
    DISCORD: "From Discord's status page",
}


@dataclass(frozen=True)
class Service:
    name: str             # canonical name in probe_results / the platform API
    label: str            # name shown on the page
    what: str             # one sentence: what it does
    how: str              # OUTSIDE | INSIDE | DISCORD
    # Diagnostic checks (domain lookup, certificate). They can take their
    # component down when they fail outright, but they never feed its uptime
    # number and a mere warning (certificate nearing expiry) never shows as
    # customer-facing trouble.
    advisory: bool = False
    # Hidden until it first reports. For stack members that only exist in
    # some deployments and probes that need configuration.
    optional: bool = False


SERVICES: list[Service] = [
    Service("Public Site", "Website",
            "Loads yourbot.gg over the public internet, the way a visitor would.", OUTSIDE),
    Service("Dashboard", "Dashboard",
            "The web app behind sign-in and every settings page.", INSIDE),
    Service("DNS", "Domain lookup",
            "Looks up the yourbot.gg address.", OUTSIDE, advisory=True),
    Service("SSL Certificate", "Security certificate",
            "Confirms the site's HTTPS certificate is valid and not about to expire.", OUTSIDE, advisory=True),
    Service("Gateway", "Gateway",
            "Keeps the shared bot connected to Discord.", INSIDE),
    Service("Bot Worker", "Bot Worker",
            "Runs the built-in plugins when something happens in your server.", INSIDE),
    Service("Bot", "Bot",
            "Carries out actions in Discord, such as sending messages and assigning roles.", INSIDE),
    Service("Orchestrator", "Orchestrator",
            "Starts custom bots and restarts them if they crash.", INSIDE),
    Service("Plugin Runner", "Plugin Runner",
            "Runs marketplace plugins inside their sandbox.", INSIDE),
    Service("Sandbox", "Plugin dashboards",
            "Serves the dashboard pages that marketplace plugins provide.", INSIDE, optional=True),
    Service("WebSocket Broker", "WebSocket Broker",
            "Holds the live connections some marketplace plugins rely on.", INSIDE, optional=True),
    Service("Analytics", "Analytics",
            "Saves server activity for your analytics charts.", INSIDE),
    Service("Image Service", "Image Service",
            "Stores and serves uploaded images.", INSIDE, optional=True),
    Service("Database", "Database",
            "Where settings and server data are stored.", INSIDE),
    Service("Cache", "Cache",
            "Short-term storage and message queues used by every service.", INSIDE),
    Service("Dev Portal Runner", "Dev Portal Runner",
            "Runs plugins that developers are testing.", INSIDE, optional=True),
    Service("Dev Portal Bot", "Dev Portal Bot",
            "The test bot developers use to try their plugins.", INSIDE, optional=True),
    Service("FAQ Matcher", "FAQ Matcher",
            "Finds answers to questions asked in YourBot support tickets.", INSIDE, optional=True),
    Service("Discord", "Discord",
            "Discord's own report on its API and gateway.", DISCORD, optional=True),
    Service("Discord API", "Discord API",
            "Confirms Discord's API accepts requests from our monitor.", OUTSIDE, optional=True),
]

SERVICE_BY_NAME: dict[str, Service] = {s.name: s for s in SERVICES}
SERVICE_ORDER: list[str] = [s.name for s in SERVICES]
OPTIONAL_SERVICES: set[str] = {s.name for s in SERVICES if s.optional}
ADVISORY_SERVICES: set[str] = {s.name for s in SERVICES if s.advisory}


@dataclass(frozen=True)
class Group:
    key: str
    name: str
    blurb: str
    services: tuple[str, ...]
    # Counts toward the headline uptime figure: the things every server
    # owner uses. Developer and support tooling are reported but kept out
    # of that one number.
    core: bool = True
    # Not ours. Shown so a reader can tell "Discord is having trouble"
    # apart from "YourBot is", and never counted in our uptime or verdict.
    third_party: bool = False


GROUPS: list[Group] = [
    Group("website", "Website and dashboard",
          "yourbot.gg, sign-in and every settings page",
          ("Public Site", "Dashboard", "DNS", "SSL Certificate")),
    Group("bot", "YourBot in Discord",
          "The shared bot staying online in your server",
          ("Gateway",)),
    Group("commands", "Commands and automations",
          "Slash commands, moderation, welcomes, tickets and scheduled posts",
          ("Bot Worker", "Bot")),
    Group("custom-bots", "Custom bots",
          "Bots that run under your own name and avatar",
          ("Orchestrator",)),
    Group("plugins", "Marketplace plugins",
          "Plugins you install from the marketplace and their dashboards",
          ("Plugin Runner", "Sandbox", "WebSocket Broker")),
    Group("analytics", "Analytics",
          "Collecting activity for your server's charts",
          ("Analytics",)),
    Group("images", "Images",
          "Uploaded artwork and images",
          ("Image Service",)),
    Group("data", "Data storage",
          "The database and cache behind every feature",
          ("Database", "Cache")),
    Group("developer-portal", "Developer portal",
          "The test environment for plugin developers",
          ("Dev Portal Runner", "Dev Portal Bot"), core=False),
    Group("support-assistant", "Support assistant",
          "Automatic answers in YourBot support tickets",
          ("FAQ Matcher",), core=False),
    Group("discord", "Discord",
          "Every bot depends on Discord's own service",
          ("Discord", "Discord API"), core=False, third_party=True),
]

GROUP_BY_KEY: dict[str, Group] = {g.key: g for g in GROUPS}
_GROUP_OF_SERVICE: dict[str, Group] = {name: g for g in GROUPS for name in g.services}

# A confirmed failure of one of these is a major outage, not a partial one:
# the website cannot be reached, the shared bot is offline, or the data
# every feature needs is unavailable.
CRITICAL_SERVICES: set[str] = {"Public Site", "Gateway", "Database"}

# Never part of OUR uptime: other people's services and diagnostics.
UPTIME_EXCLUDED_SERVICES: tuple[str, ...] = tuple(
    s.name for s in SERVICES
    if s.advisory or (_GROUP_OF_SERVICE.get(s.name) is not None and _GROUP_OF_SERVICE[s.name].third_party)
)


def group_of(service_name: str) -> Group | None:
    return _GROUP_OF_SERVICE.get(service_name)


def service_label(service_name: str) -> str:
    svc = SERVICE_BY_NAME.get(service_name)
    return svc.label if svc else service_name


def slugify(name: str) -> str:
    return name.lower().replace(" ", "-")
