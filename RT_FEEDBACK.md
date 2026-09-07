# Railtracks feedback

Notes gathered while building Emalia (an email agent) against `railtracks`
1.5.3. Ordered roughly by how much friction each one caused. Nothing here
blocked the build; all of it is either a docs gap, an ergonomics wrinkle, or a
place where the framework quietly does something other than what a reasonable
reader would expect.

Context for anything that needs it: Emalia is a long-running daemon. One agent
node is built at startup and invoked once per incoming email, potentially
thousands of times over days.

---

## 1. `MaxCalls` counts for the life of the node, which makes it unusable in a daemon

**Severity: high — this is a correctness trap, not a papercut.**

`prebuilt.middleware.MaxCalls` reads as "limit how much this agent can do", and
the docstring says "The count is tracked per `MaxCalls` instance". What that
means in practice is that the counter is bound to the *node class*, which for
any long-lived process is bound to the process.

I wanted "at most 25 tool calls per incoming email". The obvious spelling:

```python
Agent = rt.agent_node(..., model_middleware=[MaxCalls(25)])
```

does not do that. It means "at most 25 model calls ever", so the daemon works
for the first few emails and then refuses everything forever, with a
`MaxCallsExceededError` that gives no hint that the budget was consumed by
*previous, unrelated* invocations.

That failure mode is nasty: it passes every test, works in a demo, and breaks
in production a few hours in.

I ended up not using the middleware at all and enforcing the budget in my own
tool wrapper against a counter I reset per email
(`emalia/security/policy.py::charge_tool_call`).

**Suggestions, in order of preference:**

1. Make the scope explicit in the API: `MaxCalls(25, scope="per_invocation")`
   with `scope="lifetime"` as the other option. Even better, make
   `per_invocation` the default, since that is what people mean.
2. Failing that, a `reset()` method on the instance would at least let a caller
   manage it. Right now the counter is private (`_call_count`) with no public
   way to clear it.
3. At minimum, the docstring and the docs page should say, in those words,
   that the budget is *not* per invocation and that a long-running process will
   exhaust it. "Tracked per `MaxCalls` instance" is technically true and
   practically misleading, because the reader's mental model of an "instance"
   is the run, not the class.

The same concern applies to `Lock` and any other stateful prebuilt middleware,
though those hurt less.

---

## 2. No documented way to bound tool calls within one invocation

Related to the above, but worth separating: the natural thing to want from an
agent framework is "don't let this agent loop more than N times on one
request". As far as I could find, there is no first-class way to express that.

`model_middleware=[MaxCalls(n)]` is the closest thing and has the lifetime
problem above. A `max_iterations` parameter on `agent_node`, scoped to the
tool-calling loop, would cover the case everyone actually has.

---

## 3. `Flow.connect()` per concurrent invocation is a sharp edge

The flows doc says:

> A connection handles one invocation at a time and raises if you start a
> second while the first is in flight. Open a connection per concurrent run.

That is clear once you find it, but the failure mode if you miss it is a
runtime exception under concurrency only, which is exactly the condition least
likely to be covered by a test. I now unconditionally do
`self._flow.connect().ainvoke(...)` per email, even in the serial path, purely
so I never have to think about it again.

**Suggestion:** either make `Flow.ainvoke` internally take a connection per
call (so `Flow` is trivially concurrency-safe and `connect()` is only for
introspection), or raise an error message that names the fix. If the current
behaviour is deliberate, the "Concurrency" note deserves to be higher on the
page than "Inspecting a Run" — it is a correctness issue, not an advanced
feature.

---

## 4. Return type of `flow.invoke` / `ainvoke` is under-documented

The quickstart does `print(response)` and the README does `print(result.text)`.
Neither says what the object is or when `.text` exists.

Reading the source, it is a `StringResponse` for a tool-calling or plain agent
and a `StructuredResponse` for a schema agent, and `.text` raises `TypeError`
if the content is not a string. I wrote defensively:

```python
text = getattr(response, "text", None)
return text if isinstance(text, str) and text.strip() else str(response)
```

which is more ceremony than it should be.

**Suggestion:** state the return type in the `Flow.invoke` docstring and in the
flows doc, with a one-line table of which response type comes from which agent
shape. The `LLMResponse` / `StringResponse` / `StructuredResponse` hierarchy is
good; it is just not surfaced where a new user reads.

---

## 5. `agent_node` returns a class, and nothing in the signature says so

The skill doc mentions this in a parenthetical ("it returns a class/type, so
use PascalCase"), and `AGENTS.md` repeats it. The signature's return type is
`type[Node[...]]`, which is correct but easy to skim past.

This tripped me up in a specific way: I wanted to pass a per-invocation object
(the address currently being replied to) into the tools, and my first instinct
was to construct the agent per email. That works but rebuilds the model client
each time. What I actually did was close over a mutable holder in the tool
closures and mutate it before each invocation
(`EmailTools.bind_reply_target`), which works but feels like fighting the
grain.

**Suggestion:** `rt.context` is presumably the intended answer here, but the
context docs frame it as "shared information across a run" rather than "the way
to give a tool per-invocation state it cannot take as a parameter". A worked
example of that would have saved me an hour. Specifically: a tool whose
behaviour depends on who the request came from, where that identity must not be
a model-supplied parameter because the model could lie about it.

That pattern — **request identity that the model must not control** — is
central to any agent exposed to untrusted input, and I could not find it in the
docs at all.

---

## 6. `function_node` on a closure works, but is undocumented

Every one of my tools is a nested function returned by a builder method, so it
can close over a mail client and a policy without those becoming
model-visible parameters:

```python
def _read_file(self):
    policy = self.policy

    @tool_result(policy)
    def read_file(path: str, max_lines: int = 0) -> str:
        """..."""
    return read_file
```

This works perfectly — schema, name and description all come out right, and
`functools.wraps` in my decorator preserves what railtracks reads. But every
example in the docs is a module-level function, so I did not know whether it
would work until I tried it.

**Suggestion:** one example of a factory-built tool in the function-tools doc.
It is the natural way to give tools dependencies without a global, and I
suspect most non-trivial users end up there.

Relatedly: it would help to state explicitly that railtracks reads
`__name__`, `__doc__` and `__annotations__` off the callable, so a decorator
must use `functools.wraps`. I guessed right, but a decorator that dropped the
docstring would produce an agent that silently misuses its tools rather than
an error.

---

## 7. `from __future__ import annotations` and the tool schema

My tool modules use postponed annotations, so `__annotations__` values are
strings (`'str'`, not `str`). railtracks handles this correctly — the schemas
came out right — but I only established that by building an agent and reading
`tool_info()`.

**Suggestion:** worth one sentence in the function-tools doc confirming that
postponed annotations are supported. It is the default style in a lot of
modern codebases and "does my type hint reach the model" is not something a
user should have to verify empirically.

---

## 8. Small things

- **`agent_node(tool_nodes=[])`** — an empty list is falsy but not `None`, and
  I was not sure whether it would build a plain chat agent or a tool agent with
  no tools. I defensively wrote `tool_nodes=tool_nodes or None`. Worth a
  sentence.
- **`rt.llm.*` constructor kwargs** are not documented in one place. I passed
  `temperature` and `max_tokens` positionally-by-keyword and they were
  accepted, but I could not find a list of what each provider client takes.
  `docs/documentation/agent_design/llms/common_hyperparams.md` exists but does
  not enumerate per-provider support.
- **Import time is significant.** Measured on this machine, Python 3.12,
  railtracks 1.5.3, warm filesystem cache:

  | Import | Cold |
  |---|---|
  | `emalia` + `emalia.mail` + `emalia.security` (no railtracks) | 0.47 s |
  | first `import railtracks` | 17.6 s |

  That is large enough that I restructured my package root to import
  railtracks lazily, so someone using only the mail toolkit does not pay for
  it. It also makes the CLI feel broken on first run — `emalia check` sits
  silent for the better part of twenty seconds before printing anything.

  I have not profiled where it goes, but provider SDKs are the obvious
  suspect: `anthropic`, `openai` and friends are all imported eagerly through
  `railtracks.llm` even when only one is used. Deferring each provider client's
  SDK import until the client is constructed would probably recover most of
  it, and would be felt by every CLI-shaped consumer.
- **`AGENTS.md` mentions `railtracks add claude:agent-builder`** for getting
  the skill doc. That was genuinely useful and I would surface it in the main
  README, not only in the contributor-facing file.

---

## What worked well

Worth saying, since the above is all complaints:

- **`@rt.function_node` on a plain function is exactly right.** Type hints plus
  a Google docstring, no schema to hand-write, no registration step. Adding a
  tool to Emalia is writing a function.
- **`agent_node` auto-selecting behaviour from what you pass** meant I never
  had to learn a taxonomy of agent classes.
- **The `tool_nodes()` / `tool_info()` introspection** made it straightforward
  to write a test asserting that a disabled toolset really is absent from the
  schema — which is a security property in my case, so being able to test it
  mattered.
- **Refusing `tool_nodes` and `output_schema` together**, with a reason in the
  error, is the kind of guard rail that saves people from a subtle failure.
- **The whole thing is debuggable.** Setting a breakpoint in a tool and getting
  a normal Python stack is not a given in this space.
