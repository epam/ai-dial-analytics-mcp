# Deploying ADAS MCP and configuring AI query helper

This guide is for environments that do not run the DIAL Deployment Manager (DM). It covers the **read-only (`limited`)** adas-mcp instance.
This instance is intended to be used by AI Query Helper application, not by coding agents.

## TL;DR

The published image can be deployed directly; no extra wrapping is needed. The server speaks MCP streamable HTTP on its own, so you need a Deployment, a Service, an ingress, and a toolset registered in DIAL Core.

## Reference configuration

| Setting             | Value                                                      |
|---------------------|------------------------------------------------------------|
| Image               | `ghcr.io/epam/ai-dial-analytics-mcp:<version>`             |
| Command / args      | image default: `adas-mcp --transport http`, so no override |
| Transport           | MCP streamable HTTP                                        |
| Container port      | `5000`                                                     |
| Endpoint path       | `/mcp`                                                     |
| CPU / memory        | 0.5 CPU, 1 GiB (requests = limits)                         |
| Replicas            | 1                                                          |
| `ADAS_BASE_URL`     | ADAS backend URL                                           |
| `ADAS_CAPABILITIES` | `limited`                                                  |

## Steps

### 1. Get the image

Use the released image, `ghcr.io/epam/ai-dial-analytics-mcp:<version>` (also on Docker Hub as `epam/ai-dial-analytics-mcp`). It is built for `linux/amd64` and `linux/arm64`.

- Use a **fixed version tag**, never a moving one such as `latest` or `development`.
- To build it yourself instead, use this repo's `Dockerfile` (`docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/adas-mcp:<version> .`) and push it to a registry the cluster can pull from, adding an `imagePullSecret` if that registry needs credentials. Dependencies come from the committed `uv.lock`, so builds of the same commit install the same packages.

### 2. Create the Deployment and Service

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: adas-mcp
spec:
  replicas: 1
  selector:
    matchLabels: { app: adas-mcp }
  template:
    metadata:
      labels: { app: adas-mcp }
    spec:
      automountServiceAccountToken: false
      # imagePullSecrets: [{ name: <pull-secret> }]   # only for a private registry
      containers:
        - name: adas-mcp
          image: ghcr.io/epam/ai-dial-analytics-mcp:<version>
          ports:
            - containerPort: 5000
          env:
            - name: ADAS_BASE_URL
              value: "https://<adas-backend-host>"
            - name: ADAS_CAPABILITIES
              value: "limited"
          resources:
            requests: { cpu: 500m, memory: 1Gi }
            limits:   { cpu: 500m, memory: 1Gi }
          readinessProbe:
            tcpSocket: { port: 5000 }
          livenessProbe:
            tcpSocket: { port: 5000 }
---
apiVersion: v1
kind: Service
metadata:
  name: adas-mcp
spec:
  selector: { app: adas-mcp }
  ports:
    - port: 80
      targetPort: 5000
```

Notes:

- **Probes:** there is no HTTP health endpoint, so use TCP probes. Do not send an HTTP GET to `/mcp`: it is not a plain GET endpoint and the probe would fail.
- **Replicas:** keep `replicas: 1`. FastMCP keeps per-client sessions (`Mcp-Session-Id`) in memory and adas-mcp doesn't switch to stateless mode.
- **Do not set:** a `command`/`args` override, or `FASTMCP_HOST`/`FASTMCP_PORT`. The image already sets these to `0.0.0.0:5000`.
- **Knative instead:** a Knative Service works too. It is not required.

### 3. Optional env vars

| Variable             | Default | When to set                                                                                                                                                              |
|----------------------|---------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ADAS_HTTP_TIMEOUT`  | `30`    | Timeout (seconds) for calls to the ADAS backend                                                                                                                          |
| `ADAS_API_KEY`       | none    | Fallback admin key, used only when a request carries neither a user JWT nor an `Api-Key` header. Not needed behind DIAL. Store it in a Secret (`valueFrom.secretKeyRef`) |
| `OTEL_*`             | exporters `none` | Standard OpenTelemetry configuration. The image runs under `opentelemetry-instrument`, so setting `OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` plus `OTEL_EXPORTER_OTLP_ENDPOINT` (grpc or http/protobuf) is all it takes. `prometheus` in `OTEL_METRICS_EXPORTER` serves `:9464/metrics`. Expose that port on the Service for a ServiceMonitor. See [observability](observability.md) |

Leave `ADAS_CAPABILITIES=limited` and leave `ADAS_ENABLE_EXECUTE_SQL` unset. Those two settings are what keep this instance read-only.

### 4. Expose the endpoint

Expose the Service through the environment's Ingress or Route. The MCP endpoint is:

```
https://<host>/mcp
```

The ingress must:

- pass the `Api-Key` header (the per-request key from DIAL, see step 6) through unchanged;
- not buffer responses, and use generous read/idle timeouts. Streamable HTTP can hold responses open.

### 5. Allow outbound traffic

If the cluster enforces network policies, allow egress from the pod to the `ADAS_BASE_URL` host on port 443, plus DNS. With telemetry enabled, also allow egress to the collector's OTLP port (4317 for grpc, 4318 for http/protobuf).

### 6. Register in DIAL Core

Register the server in DIAL Core as a toolset. Use the admin UI or the toolset config, with these settings:

| Setting                           | Value | Why |
|-----------------------------------|---|---|
| Toolset ID                        | `adas-mcp` | AI Query Helper references the toolset by this id, and its prompt calls the tools as `ADAS_MCP_adas_*`. Use exactly this id |
| `displayName`                     | `ADAS MCP` | |
| `endpoint`                        | `https://<host>/mcp`, or the in-cluster `http://adas-mcp.<namespace>.svc.cluster.local/mcp` | The full MCP URL, including `/mcp` |
| `transport`                       | `HTTP` | adas-mcp serves streamable HTTP. `SSE` will not work |
| `forwardPerRequestKey`            | `true` | DIAL sends a per-request key, bound to the calling user, in the `Api-Key` header. adas-mcp passes it on to ADAS, so every ADAS call runs **as the calling user** |
| `forwardAuthToken`                | `false` | Not needed: the per-request key already carries the user's identity |
| `authSettings.authenticationType` | `none` | The toolset needs no OAuth or API key of its own; the per-request key is the credential |

Example:

```json
{
  "displayName": "ADAS MCP",
  "endpoint": "https://<host>/mcp",
  "transport": "HTTP",
  "forwardPerRequestKey": true,
  "forwardAuthToken": false,
  "authSettings": { "authenticationType": "none" }
}
```

Notes:

- **Roles:** use `userRoles` to restrict which DIAL users can see the toolset, if needed.
- **ADAS permissions:** ADAS applies the user's own permissions, so each user sees only the tables and pipelines they can access in ADAS.

## Verification

1. Check the pod is `Running`/`Ready` and its logs show the server listening on `0.0.0.0:5000`.
2. Port-forward and send an MCP `initialize`:

   ```bash
   kubectl port-forward svc/adas-mcp 5001:80
   curl -s http://localhost:5001/mcp \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
   ```

   A successful response contains `"serverInfo"`. The response headers include `Mcp-Session-Id`.
3. After registering in DIAL, check that the tool list contains only read-only tools. No write tools and no `adas_execute_sql` should appear.

## Next step: create AI Query Helper

Start this step only once the toolset passes the checks above. AI Query Helper (`adas-query-helper`) is a DIAL Quick App that helps users of the analytics UI write ADAS SQL. It composes and validates queries through the `adas-mcp` toolset but never runs them.

### Prerequisites

- **The `adas-mcp` toolset**, registered and verified as above, under exactly that id.
- **The Quick Apps service** (`quickapps2`) deployed, with its application type schema `https://mydial.epam.com/custom_application_schemas/quickapps2` registered in DIAL Core.
- **An orchestrator model:** a capable tool-calling chat model deployed in DIAL Core, e.g. Claude Sonnet 4 (`anthropic.claude-sonnet-4-20250514-v1:0`). See [Configuration](#configuration).
- **The server settings unchanged:** `ADAS_CAPABILITIES=limited`, and `ADAS_ENABLE_EXECUTE_SQL` unset. Every tool the app uses is in the read-only set, and its prompt assumes it cannot run SQL.

### Create the app

Create a Quick App 2.0 application, either in DIAL Chat / the admin UI or with `PUT /v1/applications/<bucket>/<path>` on DIAL Core. Name, folder and version are up to you; the full id has the form `applications/<bucket>/<folder>/<name>__<version>`, e.g. `applications/public/ADAS/adas-query-helper__1.0.0`. The analytics UI finds the app by its full id, set in `DIAL_QUERY_ASSISTANT_DEPLOYMENT` (see [Connect the analytics UI](#connect-the-analytics-ui)), so any path works as long as the two match.

Settings:

| Setting                | Value                                                                                                         |
|------------------------|---------------------------------------------------------------------------------------------------------------|
| Display name / version | e.g. `adas-query-helper` / any version you choose                                                             |
| Application type       | Quick App 2.0 (`application_type_schema_id`: `https://mydial.epam.com/custom_application_schemas/quickapps2`) |
| Orchestrator model     | the model's DIAL deployment id, temperature `0.5`                                                             |
| System prompt          | custom: the full content of [`query-helper-system-prompt.md`](query-helper-system-prompt.md), pasted verbatim |
| Toolsets               | the MCP toolset `adas-mcp`                                                                                    |
| Timestamp feature      | enabled, injected as a tool call                                                                              |
| Forward auth token     | `false`                                                                                                       |

Equivalent manifest (`application_properties.orchestrator.system_prompt.content` holds the whole prompt file as one JSON string):

```json
{
  "display_name": "adas-query-helper",
  "display_version": "<version>",
  "forward_auth_token": false,
  "features": {
    "assistant_attachments_in_request_supported": true,
    "skills_supported": true
  },
  "application_type_schema_id": "https://mydial.epam.com/custom_application_schemas/quickapps2",
  "application_properties": {
    "orchestrator": {
      "deployment": {
        "deployment_id": "<model-deployment-id>",
        "parameters": { "temperature": 0.5 }
      },
      "system_prompt": {
        "type": "custom",
        "variables": {},
        "content": "<contents of query-helper-system-prompt.md>"
      },
      "attachment_strategy": null
    },
    "contexts": [],
    "tool_sets": [
      { "type": "dial-mcp", "deployment_id": "adas-mcp" }
    ],
    "conversation_starters": null,
    "features": {
      "timestamp": { "injection_strategy": "tool_call" },
      "dial_files": null,
      "representation_tooling": null,
      "web_fetch": null
    }
  }
}
```

To build the JSON string from the prompt file:

```bash
jq -Rs . docs/query-helper-system-prompt.md
```

### Connect the analytics UI

Set the admin-frontend env var to the app's full id:

```
DIAL_QUERY_ASSISTANT_DEPLOYMENT=applications/<bucket>/<folder>/<name>__<version>
```

For example `applications/public/ADAS/adas-query-helper__1.0.0`. The value must match the app id exactly, version suffix included. Redeploy or restart the frontend for the new value to take effect.

### Configuration

- **Toolset id:** keep it `adas-mcp`. The app references it in `tool_sets`, and the prompt calls every tool by the `ADAS_MCP_` prefix (e.g. `ADAS_MCP_adas_validate_sql`). Renaming the toolset means updating both the manifest and the prompt.
- **Model:** use any capable tool-calling chat model available in the environment. Set it in `deployment_id`; the prompt doesn't depend on a particular model.
- **Prompt updates:** `query-helper-system-prompt.md` is the source of truth. When it changes, re-paste the prompt. If you also bump `display_version`, the app id changes, so update `DIAL_QUERY_ASSISTANT_DEPLOYMENT` to match.

### Verify the app

1. Open the app in DIAL Chat and ask: "Which table holds token counts per request?" It should answer from the catalog, using the `ADAS_MCP_adas_list_entities` / `ADAS_MCP_adas_describe_entity` tools.
2. Ask for a query, e.g. "total cost per deployment for last week". The answer should be one SQL block, preceded by a successful `ADAS_MCP_adas_validate_sql` call, with the date window and row limit stated.
3. Ask it to run the query or to delete a table. It should decline in one sentence.
4. Open the query helper from the analytics UI. If it does not appear or fails to load, check that `DIAL_QUERY_ASSISTANT_DEPLOYMENT` matches the app id exactly, and that the user can access the app.
