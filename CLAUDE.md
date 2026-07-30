# CLAUDE.md — hermes-render-mrd4v6qq

See @SERVICES.md for connecting to admin services like SSH for accessing a Render service.

See ARCHITECTURE.md for notes on architecture, architectural patterns, environment variables, and communication platform integrations (like LINE). Always make updating ARCHITECTURE.md a part of your process when implementing relvant changes.

See @UPGRADING.md before touching `ARG HERMES_IMAGE` in the Dockerfile. Bumping the upstream Hermes version is never a one-line change — start with `./scripts/upgrade-preflight.sh <new-tag>`.

## About NGraph

NGraph is a company that focuses on bespoke AI integrations for businesses primarily based in Fukui, Japan, tho it strives to address a much wider market. Our broader goal is to develop a SaaS offering spawned from our experience building solutions for businesses and supplant our integrations business with one that is more scaleable. 

NGraph members:
Singo Takahashi -- CEO
Matt Chana -- CTO

## What this repo is

A Docker template for deploying one [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance per client business on Render. Each deployed instance is client-facing. **Only admins provision or manage Render resources** — never a deployed agent instance. 

## Client-facing Hermes Agents

- are exposed to authorized users via platforms like LINE and Telegram
