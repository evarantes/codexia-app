# Root cause — Claude Sonnet 5

Claude Sonnet 5 has adaptive thinking enabled by default when the `thinking` field is omitted. The Codexia cinematic director previously requested a long structured output with only 18k maximum output tokens. Since `max_tokens` covers thinking plus final response, a long director request can finish without a usable text block.

For deterministic structured planning, Codexia now turns thinking off and reserves the output budget for the JSON plan. This is intentionally scoped to the director endpoint and does not change other Claude/OpenRouter uses in the application.
