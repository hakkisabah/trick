"""Prove the card's jump against two stub windows, without a browser.

The card is rendered by Open WebUI inside an iframe that does not share the
page's origin (``iframeSandboxAllowSameOrigin`` defaults to false in 0.12.4), so
``window.parent.document`` throws there. The jump therefore crosses a
postMessage bridge: the card asks, a listener installed in the chat page (through
``__event_call__`` type 'execute') scrolls and answers.

This harness runs both scripts in separate contexts and wires the two windows
together, which is the part a unit test of either script alone cannot see:
a request with no reply, a reply for the wrong message id, and a reply that
arrives after the card already fell back.

Skipped when node is absent (the gate reports the skip instead of passing).
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys

ACTION = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "openwebui" / "trick_action.py"

HARNESS = r"""
const vm = require('vm');

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function makeEmitter() {
  const map = {};
  return {
    addEventListener: function (name, fn) { (map[name] = map[name] || []).push(fn); },
    removeEventListener: function (name, fn) { map[name] = (map[name] || []).filter(function (f) { return f !== fn; }); },
    dispatch: function (name, event) { (map[name] || []).slice().forEach(function (f) { f(event); }); },
    count: function (name) { return (map[name] || []).length; }
  };
}

function makeEl(tag, attrs) {
  const element = {
    tagName: tag,
    id: (attrs && attrs.id) || '',
    listeners: {},
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
      if (selector === '.jump-status') { return this.status || null; }
      if (selector === 'details.full') { return this.full || null; }
      return null;
    }
  };
  return element;
}

function run(scenario) {
  const timers = [];
  const card = makeEl('details', { id: 'trick-card' });
  card.open = true;
  const status = makeEl('div', {});
  const full = makeEl('details', {});
  const row = makeEl('li', { class: 'hit', 'data-mid': scenario.mid, 'data-cid': scenario.cid });
  row.status = status;
  row.full = full;

  const message = makeEl('div', { id: scenario.domId || 'message-none' });

  // --- chat page side ---------------------------------------------------
  const hostEvents = makeEmitter();
  const hostDocument = {
    getElementById: function (id) { return id === message.id ? message : null; },
    querySelectorAll: function (selector) { return selector === '[id^="message-"]' ? [message] : []; }
  };
  const hostWindow = {
    addEventListener: hostEvents.addEventListener,
    removeEventListener: hostEvents.removeEventListener,
    dispatch: hostEvents.dispatch
  };
  const hostSandbox = { window: hostWindow, document: hostDocument, console: console };
  hostSandbox.globalThis = hostSandbox;
  vm.createContext(hostSandbox);
  const hostResult = vm.runInContext(scenario.host, hostSandbox);

  // --- card side --------------------------------------------------------
  const cardEvents = makeEmitter();
  const cardDocument = {
    getElementById: function (id) { return id === 'trick-card' ? card : null; },
    querySelectorAll: function (selector) { return selector === 'li.hit' ? [row] : []; },
    addEventListener: function () {},
    body: { scrollHeight: 100 }
  };
  const cardWindow = {
    addEventListener: cardEvents.addEventListener,
    removeEventListener: cardEvents.removeEventListener,
    dispatch: cardEvents.dispatch
  };
  const parent = {
    postMessage: function (data) {
      if (scenario.noListener) { return; }
      hostEvents.dispatch('message', { data: clone(data), source: cardWindow });
    }
  };
  if (scenario.direct) {
    parent.document = hostDocument;
  } else if (scenario.blocked) {
    Object.defineProperty(parent, 'document', {
      get: function () { throw new Error('SecurityError: cross-origin frame'); }
    });
  }
  cardWindow.parent = parent;
  cardWindow.postMessage = function (data) {
    cardEvents.dispatch('message', { data: data, source: hostWindow });
  };
  const cardSandbox = {
    window: cardWindow,
    document: cardDocument,
    parent: parent,
    setTimeout: function (fn) { timers.push(fn); return timers.length; },
    console: console
  };
  cardSandbox.globalThis = cardSandbox;
  vm.createContext(cardSandbox);
  vm.runInContext(scenario.script, cardSandbox);

  const handlers = row.listeners['click'] || [];
  if (handlers.length !== 1) { throw new Error('row got no click handler'); }
  handlers[0].call(row, { target: row, preventDefault: function () {} });

  const statusAfterClick = status.textContent;
  timers.splice(0).forEach(function (fn) { fn(); });

  return {
    hostInstalled: hostResult === true,
    hostListeners: hostEvents.count('message'),
    scrolled: message.scrolled,
    block: message.scrolledBlock,
    animated: message.animated,
    detailsOpen: full.open,
    statusAfterClick: statusAfterClick,
    statusAfterTimers: status.textContent
  };
}

const payload = JSON.parse(process.argv[2]);
const results = {};
for (const name of Object.keys(payload.scenarios)) {
  results[name] = run({ script: payload.script, host: payload.host, ...payload.scenarios[name] });
}
process.stdout.write(JSON.stringify(results));
"""


def extract(name: str) -> str:
    text = ACTION.read_text(encoding="utf-8")
    match = re.search(name + r' = """(.*?)"""', text, re.S)
    assert match, f"{name} is missing from the action module"
    value = match.group(1).strip()
    if name == "SCRIPT":
        value = re.sub(r"^<script>|</script>$", "", value).strip()
    return value


def main() -> int:
    if shutil.which("node") is None:
        print("skip: node is not installed, so the two-window jump harness cannot run")
        return 0

    script = extract("SCRIPT")
    host = extract("HOST_JS")
    assert "postMessage" in script and "trick-card" in script
    assert "scrollIntoView" in host and "trick-host" in host

    payload = {
        "script": script,
        "host": host,
        "scenarios": {
            # The listener finds the message: the page scrolls, the card does not
            # open its fallback, and the row says so.
            "host_finds": {"cid": "chat-9", "mid": "a-2", "domId": "message-chat-9-a-2", "blocked": True},
            # The message is not in the DOM (virtualised long chat): the listener
            # answers "no", and the card opens the passage instead of nothing.
            "host_misses": {"cid": "chat-9", "mid": "a-2", "domId": "message-chat-9-b-7", "blocked": True},
            # No listener at all (an older bridge, a client without execute):
            # the wait runs out and the fallback still happens.
            "no_host": {"cid": "chat-9", "mid": "a-2", "domId": "message-chat-9-a-2", "blocked": True, "noListener": True},
            # A result without a message id cannot be jumped to at all.
            "no_mid": {"cid": "chat-9", "mid": "", "domId": "message-chat-9-a-2", "blocked": True},
            # Same-origin frame (standalone use, or an admin enabled the flag):
            # the direct path is used and no bridge round trip happens.
            "same_origin": {"cid": "chat-9", "mid": "a-2", "domId": "message-chat-9-a-2", "direct": True},
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
    found = results["host_finds"]
    if not found["hostInstalled"]:
        failures.append("the host listener did not install")
    if not found["scrolled"]:
        failures.append("a message the page can reach was not scrolled")
    if found["block"] != "center":
        failures.append(f"scroll block was {found['block']!r}, expected 'center'")
    if not found["animated"]:
        failures.append("the scrolled message was not flashed")
    if found["detailsOpen"]:
        failures.append("the fallback opened even though the jump worked")
    if "jumped" not in found["statusAfterClick"]:
        failures.append(f"the row said {found['statusAfterClick']!r} after a successful jump")

    missed = results["host_misses"]
    if missed["scrolled"]:
        failures.append("a missing message scrolled something else")
    if not missed["detailsOpen"]:
        failures.append("a missing message did not open the passage")
    if "not on screen" not in missed["statusAfterTimers"]:
        failures.append(f"a miss said {missed['statusAfterTimers']!r}")

    orphan = results["no_host"]
    if orphan["scrolled"]:
        failures.append("a card with no host scrolled something")
    if not orphan["detailsOpen"]:
        failures.append("a card with no host did not fall back after the wait")

    nameless = results["no_mid"]
    if nameless["scrolled"]:
        failures.append("a row without a message id scrolled something")
    if not nameless["detailsOpen"]:
        failures.append("a row without a message id did not open the passage")
    if "no message id" not in nameless["statusAfterClick"]:
        failures.append(f"a row without an id said {nameless['statusAfterClick']!r}")

    direct = results["same_origin"]
    if not direct["scrolled"]:
        failures.append("a same-origin frame did not scroll directly")
    if direct["detailsOpen"]:
        failures.append("a same-origin jump also opened the fallback")

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print(
        "jump bridge ok: host scrolls and replies, a miss and a missing host both "
        "open the passage (4 paths)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
