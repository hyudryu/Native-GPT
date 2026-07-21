# Native GPT tools

Every Strands tool lives in its own folder under this directory:

```text
tools/
  tool-slug/
    manifest.json
    tool.py
    ...tool-owned downloads and data...
```

`manifest.json` describes the tool in the Native GPT Tools screen. `tool.py`
exports a `TOOL` value accepted by the Strands `Agent(tools=[...])` API. Keeping
tool-owned assets in the same folder prevents runtime downloads from cluttering
the repository.
