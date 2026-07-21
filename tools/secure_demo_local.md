# Secure local demo identities

The YouTube demo should use the same identity shape as deployment: one
CockroachDB principal per logical node. This is deliberately not the old
single-DSN shortcut.

For a three-agent synthetic run, create these principals in a disposable local
cluster before applying authority grants:

```sql
CREATE USER demo_seeder;
CREATE USER demo_agent_0;
CREATE USER demo_agent_1;
CREATE USER demo_agent_2;
CREATE USER demo_resolver;
GRANT ALL ON DATABASE stigmergy TO demo_seeder, demo_agent_0, demo_agent_1,
  demo_agent_2, demo_resolver;
GRANT ALL ON SCHEMA public TO demo_seeder, demo_agent_0, demo_agent_1,
  demo_agent_2, demo_resolver;
GRANT ALL ON ALL TABLES IN SCHEMA public TO demo_seeder, demo_agent_0,
  demo_agent_1, demo_agent_2, demo_resolver;
```

Register each matching principal in `agent_nodes`, then issue only the required
regional/global grants through the audited authority API. The seeder is the
trusted bootstrap controller; agents receive only recall, observe, reinforce,
and signal rights for their intended synthetic regions; the resolver receives
only resolve/maintenance rights.

This is a local development analogue of separate CockroachDB Cloud service
accounts and Lambda execution identities. It is not a Cloud deployment guide.
