"""Prove the card's jump script against a stub DOM, without a browser.

The card is rendered by Open WebUI inside a sandboxed iframe, so the click path
cannot be exercised with the tools available here (no browser on this host).
What can be checked is the script's own logic: given an element with the id the
chat uses, does it scroll that element; and when the parent document is
unreachable, does it fall back to opening the passage instead of doing nothing.
"""

import json
import pathlib
import re
import subprocess
import sys

ACTION = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "openwebui" / "trick_action.py"

HARNESS = r"""
const vm = require('vm');

function makeEl(tag, attrs) {
  const element = {
    tagName: tag,
    id: (attrs && attrs.id) || '',
    listeners: {},
    children: [],
    scrolled: false,
    scrolledBlock: null,
    animated: false,
    open: false,
    textContent: '',
    addEventListener: function (name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
    getAttribute: function (name) { return (attrs && attrs[name] !== undefined) ? attrs[name] : null; },
    scrollIntoView: function (options) { this.scrolled = true; this.scrolledBlock = options && options.block; },
    animate: function () { this.animated = true; },
    closest: function () { return null; },
    querySelector: function (selector) {
      if (selector === '.jump-status') { return this.status; }
      if (selector === 'details.full') { return this.full; }
      return null;
    }
  };
  return element;
}

function run(scenario) {
  const card = makeEl('details', { id: 'trick-card' });
  card.open = true;

  const status = makeEl('div', {});
  const full = makeEl('details', {});
  const row = makeEl('li', { class: 'hit', 'data-mid': scenario.mid, 'data-cid': scenario.cid });
  row.status = status;
  row.full = full;

  const chatMessage = makeEl('div', { id: scenario.domId });

  const listeners = {};
  const document = {
    getElementById: function (id) { return id === 'trick-card' ? card : null; },
    querySelectorAll: function (selector) {
      if (selector === 'li.hit') { return [row]; }
      if (selector === '[id^="message-"]') { return []; }
      return [];
    },
    addEventListener: function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    body: { scrollHeight: 100 }
  };

  const parentDocument = {
    getElementById: function (id) { return id === scenario.domId ? chatMessage : null; },
    querySelectorAll: function () { return []; }
  };

  const parentObject = scenario.blocked
    ? { get document() { throw new Error('cross-origin frame'); } }
    : { document: parentDocument };

  const window = {
    addEventListener: function () {},
    parent: parentObject
  };
  window.parent.postMessage = function () {};

  const sandbox = { document: document, window: window, parent: parentObject, console: console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scenario.script, sandbox);

  const handlers = row.listeners['click'] || [];
  if (handlers.length !== 1) { throw new Error('row got no click handler'); }
  handlers[0].call(row, { target: row, preventDefault: function () {} });

  return {
    scrolled: chatMessage.scrolled,
    block: chatMessage.scrolledBlock,
    animated: chatMessage.animated,
    detailsOpen: full.open,
    status: status.textContent
  };
}

const payload = JSON.parse(process.argv[2]);
const results = {};
for (const name of Object.keys(payload.scenarios)) {
  results[name] = run({ script: payload.script, ...payload.scenarios[name] });
}
process.stdout.write(JSON.stringify(results));
"""


def extract_script() -> str:
    text = ACTION.read_text(encoding="utf-8")
    match = re.search(r'SCRIPT = """(.*?)"""', text, re.S)
    assert match, "the card script is missing from the action module"
    script = re.sub(r"^<script>|</script>$", "", match.group(1).strip())
    assert "scrollIntoView" in script
    return script


def main() -> int:
    script = extract_script()
    payload = {
        "script": script,
        "scenarios": {
            "resolvable": {"cid": "chat-9", "mid": "a-2", "domId": "message-chat-9-a-2", "blocked": False},
            "blocked_parent": {"cid": "chat-9", "mid": "a-2", "domId": "message-chat-9-a-2", "blocked": True},
            "missing_target": {"cid": "chat-9", "mid": "a-2", "domId": "message-other", "blocked": False},
        },
    }
    harness = pathlib.Path("/tmp/trick-jump-harness.js")
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip())
        return 1
    results = json.loads(proc.stdout)

    failures = []
    ok = results["resolvable"]
    if not ok["scrolled"]:
        failures.append("a resolvable row did not scroll the chat")
    if ok["block"] != "center":
        failures.append(f"scroll block was {ok['block']!r}, expected 'center'")
    if not ok["animated"]:
        failures.append("the scrolled message was not flashed")
    if ok["detailsOpen"]:
        failures.append("the fallback opened even though the jump worked")

    blocked = results["blocked_parent"]
    if blocked["scrolled"]:
        failures.append("a blocked parent document scrolled anyway")
    if not blocked["detailsOpen"]:
        failures.append("a blocked parent did not open the full passage")
    if "could not scroll" not in blocked["status"]:
        failures.append(f"blocked status was {blocked['status']!r}")

    missing = results["missing_target"]
    if missing["scrolled"]:
        failures.append("a missing target scrolled something else")
    if not missing["detailsOpen"]:
        failures.append("a missing target did not open the full passage")

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("jump script ok: scrolls when resolvable, opens the passage when not (2 fallbacks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
