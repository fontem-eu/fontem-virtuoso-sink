# fontem-virtuoso-sink

Event consumer that projects events.entity_events into the Virtuoso RDF store. Same shape as fontem-neo4j-sink but renders triples and writes to named SPARQL graphs.

## Deploy

CI auto-deploys to the testing env on every merge to main. Promotion to staging / prod is **manual** — bump the version in `gitops/<env>/<service>.yaml` to land it in a given environment.

## Convention

See [/config/repos/CLAUDE.md](https://contribute.void42.internal/fontem/gitops) for workspace-wide rules (feature branches + CI gate, no direct push to main, full gate before declaring done, conventional commits).
