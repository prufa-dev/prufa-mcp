# Advanced configuration

## Config-as-code (`~/.config/prufa/mcp.json`)

If you'd rather not pass credentials through env vars, drop a JSON file at
`~/.config/prufa/mcp.json` (honors `XDG_CONFIG_HOME`; `PRUFA_CONFIG` overrides
the path):

```json
{
  "api_token": "your-prufa-api-key",
  "api_base": "https://app.prufa.dev"
}
```

Environment variables take precedence over the file, so `PRUFA_API_TOKEN` /
`PRUFA_API_BASE` always win when set. Both keys are optional — `api_base`
defaults to the hosted API. A malformed file is ignored with a warning on
stderr rather than crashing the server.
</content>
