# Container image for the prufa-mcp server.
#
# Primary purpose: let MCP registries (e.g. Glama) and CI build the server and
# verify it starts and answers MCP introspection (tools/list) over stdio.
# The server needs no credentials to start or introspect — an API key is only
# required at audit-call time.
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# Install from source so the image always matches this revision.
RUN pip install --no-cache-dir .

# The OSS build speaks MCP over stdio.
ENTRYPOINT ["prufa-mcp"]
