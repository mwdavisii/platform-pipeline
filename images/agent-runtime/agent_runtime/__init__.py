"""The golden road's agent runtime (spec 2026-11-17): it runs one agent.yaml with the tools its environment's bindings give it, and
nothing else. Built and signed by release-agent-runtime.yml; an agent app's image is FROM it, plus agent.yaml."""

NAME = "golden-road-agent-runtime"
