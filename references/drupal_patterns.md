# Drupal UI patterns for agent-browser

This file is a grab-bag of practical interaction patterns for Drupal admin UIs when driving them with `agent-browser`.

> Tip: prefer **semantic locators** (`find label …`, `find role …`) and `wait --load networkidle` over brittle CSS selectors.

---

## Login (standard /user/login)

```bash
agent-browser open "https://SITE/user/login"
agent-browser wait --load networkidle

# Fill by label (most robust)
agent-browser find label "Username" fill "admin"
agent-browser find label "Password" fill "admin"

# Click login button
agent-browser find role button click --name "Log in"

# Wait for navigation / toolbar
agent-browser wait --load networkidle
agent-browser wait --text "Log out"
```

If your site uses a different label (e.g. “Email address”), adjust the label string accordingly.

---

## Quick navigation

Drupal admin is very URL-addressable. For deterministic scripts, prefer direct routes:

- Content list: `/admin/content`
- Add content type list: `/node/add`
- Add Article: `/node/add/article`
- Add Basic page: `/node/add/page`
- Config overview: `/admin/config`
- Extend (modules): `/admin/modules`

```bash
agent-browser open "https://SITE/admin/content"
agent-browser wait --load networkidle
```

---

## Forms

### Fill a text field by label

```bash
agent-browser find label "Title" fill "My test page"
```

### Select a dropdown value

```bash
agent-browser find label "Text format" click
# If you know the underlying <select> value, use `select` with a selector/ref.
```

### Save / submit

Drupal usually has a primary submit button:

- “Save”
- “Save configuration”
- “Save and publish”

```bash
agent-browser find role button click --name "Save"
agent-browser wait --load networkidle
```

---

## AJAX-heavy UI

Prefer waiting on load state or a text change rather than sleeping.

```bash
agent-browser find role button click --name "Add"
agent-browser wait --load networkidle

# Or wait for a dialog:
agent-browser wait "role=dialog"
```

---

## Status messages (success/error)

Drupal core messages usually render with ARIA roles:

- success/status: `role="status"` (green)
- error: `role="alert"` (red)

Patterns to wait for:

```bash
agent-browser wait --text "has been saved"
agent-browser wait --text "has been created"
```

To read the page body around messages:

```bash
# This is often enough to verify intent without vision.
agent-browser get text "role=status"
agent-browser get text "role=alert"
```

---

## AI Agents Explorer output (DOM extraction)

The AI Agents Explorer output often lives in non-interactive `<pre>` blocks, so use `eval` to extract it.

```bash
agent-browser eval --json "(() => {const pres = Array.from(document.querySelectorAll('.explorer-messages pre')).map(p => p.textContent || ''); return {pre_texts: pres};})()"
```

If you need the selected model:

```bash
agent-browser eval --json "(() => {const s = document.querySelector('#edit-model'); if (!s) return {model: null}; const o = s.options[s.selectedIndex]; return {model: {value: o ? o.value : null, label: o ? (o.textContent || '') : null}};})()"
```

---

## CKEditor / rich text

CKEditor instances may be inside iframes or use `contenteditable`.
If the editor isn’t easily reachable through labels, fall back to CSS selectors or `eval`.

Example approach:

1. Use snapshot to find likely editable region.
2. If needed, `agent-browser eval` to set content.

---

## Backend probes (optional)

Use probe commands in Compare/Explore to capture server-side context without hard-coding environment details:

```bash
# DDEV example
--probe-cmd "ddev exec drush ws --count=50 --format=json"

# Bare metal example
--probe-cmd "drush ws --count=50 --format=json"
```


## Tabs, dialogs, and frames

- Use `agent-browser tab` to list/switch tabs.
- Use `agent-browser frame <selector>` to enter an iframe if needed.
- Use `agent-browser wait "role=dialog"` before interacting with modal forms.

---

## Sessions: isolate baseline vs modified

When you need two independent “browser worlds” (cookies/storage), use sessions:

```bash
agent-browser --session baseline open "https://SITE/user/login"
agent-browser --session modified open "https://SITE/user/login"
```

Or set `AGENT_BROWSER_SESSION` env var for subsequent commands.

---

## Useful debugging commands

```bash
agent-browser console --json
agent-browser errors --json
agent-browser trace start test_outputs/trace.zip
# ... do stuff ...
agent-browser trace stop test_outputs/trace.zip
```
