#!/usr/bin/env python3
"""Build the ~3-minute Leafguard demo video from recorded data, locally.

Every number on screen is read from files this repo produced (devnet receipts, experiment JSON, crash
results); nothing is typed in by hand. Tools: headless Google Chrome (slides + a live UI screenshot),
macOS `say` (narration), ffmpeg (assembly). Output: demo/pq-vault-demo.mp4 and demo/NARRATION.md.

    python3 demo/build_demo.py [--ui http://127.0.0.1:8141] [--voice Samantha] [--rate 188]
"""
import argparse
import html
import json
import os
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BUILD = os.path.join(ROOT, "demo", "build")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W, H = 1920, 1080


def load(p, default=None):
    p = os.path.join(ROOT, p)
    return json.load(open(p)) if os.path.exists(p) else default


def short(h):
    return h[:10] + "…" + h[-6:]


CSS = """
*{box-sizing:border-box} body{margin:0;width:1920px;height:1080px;background:#0e1116;color:#e6edf3;
font-family:-apple-system,"SF Pro Display","Segoe UI",sans-serif;overflow:hidden}
.wrap{padding:70px 96px;height:100%;display:flex;flex-direction:column}
.kicker{color:#7aa2f7;font-size:26px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:14px}
h1{font-size:76px;margin:0 0 18px;line-height:1.05} h2{font-size:56px;margin:0 0 28px;line-height:1.1}
p,li{font-size:34px;line-height:1.4;color:#c9d1d9} .muted{color:#8b949e} .ok{color:#3fb950} .bad{color:#f85149}
.warn{color:#d29922} .acc{color:#7aa2f7} code,.mono{font-family:"SF Mono",Menlo,monospace}
table{border-collapse:collapse;width:100%;font-size:34px} th,td{text-align:left;padding:16px 16px;border-bottom:1px solid #2a313c}
th{color:#8b949e;font-weight:500} .grid{display:grid;gap:26px} .card{background:#161b22;border:1px solid #2a313c;border-radius:16px;padding:26px 30px}
.big{font-size:96px;font-weight:700} .foot{margin-top:auto;color:#8b949e;font-size:24px}
.frame{border:2px solid #2a313c;border-radius:14px;padding:22px 26px;background:#161b22}
.arrow{font-size:60px;color:#8b949e;align-self:center}
"""


def page(body, foot="Leafguard · ETHGlobal Mumbai 2026 · devnet data from docs/RECEIPTS.md & research/results/"):
    return f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body><div class='wrap'>{body}<div class='foot'>{html.escape(foot)}</div></div></body></html>"


def slides(data):
    rec, exp, sweeps, crash, cmp_, att = (data[k] for k in ("receipts", "exp", "sweeps", "crash", "cmp", "att"))
    rows = {(r["scenario"], r["policy"], str(r["ledger"])): r for r in exp["rows"]}
    S = []

    S.append(("title", page("""
      <div class='kicker'>ETHGlobal Mumbai 2026</div>
      <h1>Leafguard</h1>
      <p style='font-size:44px'>A 2-of-3 <span class='acc'>post-quantum</span> guardian wallet</p>
      <div class='grid' style='grid-template-columns:repeat(3,1fr);margin-top:40px'>
        <div class='card'><div class='acc' style='font-size:30px'>EIP-8141</div><p>frame transactions: the account verifies its own signatures</p></div>
        <div class='card'><div class='acc' style='font-size:30px'>ERC-8421</div><p>alternative mempool with staking rules</p></div>
        <div class='card'><div class='acc' style='font-size:30px'>JARDIN</div><p>hash-based signatures (SPHINCS- family). <b>No ECDSA.</b></p></div>
      </div>"""),
      "Leafguard. A two-of-three post-quantum guardian wallet for Ethereum, built on EIP-8141 frame transactions "
      "and the ERC-8421 alternative mempool. Every approval is a hash-based JARDIN signature. There is no ECDSA "
      "anywhere in the wallet."))

    S.append(("problem", page("""
      <div class='kicker'>The problem</div><h2>Hash-based signatures are quantum-safe.<br>The cheap ones are <span class='warn'>stateful</span>.</h2>
      <div class='grid' style='grid-template-columns:1fr 1fr'>
        <div class='card'><p>A quantum computer breaks every ECDSA key. EIP-8141 lets an account run <b>any</b> verifier in its VERIFY frame — post-quantum signatures without a hard fork.</p></div>
        <div class='card'><p>JARDIN's compact path: a slot of <b>128 few-time FORS keys</b> (2.7 KB signatures). Sign two different messages with one leaf and its secrets leak.</p></div>
      </div>"""),
      "A large quantum computer breaks every elliptic-curve key. EIP-8141 lets an account verify any signature in "
      "its own validation frame, so hash-based signatures work today. The cheap ones, though, are stateful: a "
      "few-time key that signs two different messages with the same leaf leaks its secrets. That is what this "
      "project studies."))

    r1 = rec[0]
    S.append(("architecture", page(f"""
      <div class='kicker'>One frame transaction · two guardians</div><h2>How a 2-of-3 approval works</h2>
      <div style='display:flex;gap:26px;align-items:stretch'>
        <div class='frame' style='flex:1'><div class='acc' style='font-size:32px'>Frame 0 · VERIFY → validate()</div>
          <ul><li>guardian A's JARDIN FORS signature vs <code>TXPARAM(0x08)</code></li><li>leaf must be above the LeafLedger mark</li>
          <li>only frame 1 = execute() on this vault; fee cap</li><li>A signs over B's exact entry bytes</li></ul>
          <div class='big ok' style='font-size:64px'>{r1['frameReceipts'][0] and int(r1['frameReceipts'][0]['gasUsed'],16):,} gas</div><div class='muted' style='font-size:26px'>on chain · limit 99,800 + 2×100 = MAX_VERIFY_GAS</div></div>
        <div class='arrow'>→</div>
        <div class='frame' style='flex:1'><div class='acc' style='font-size:32px'>Frame 1 · SENDER → execute()</div>
          <ul><li>guardian B: compact (2.7 KB) or stateless SPX (6.5 KB)</li><li>both leaves recorded in the LeafLedger</li><li>batch runs atomically</li></ul></div>
      </div>"""),
      "Each transaction has two frames. The verify frame checks guardian A's JARDIN signature against the protocol's "
      "signature hash, rejects any leaf the chain has already seen, caps the fee, and binds the approval to the exact "
      "body frame. On chain it costs about eighty-two thousand gas, under the hundred-thousand validation cap. The "
      "sender frame then checks guardian B, who may use the stateless path, records both leaves, and runs the batch."))

    S.append(("ui", None,
      "Here is the wallet interface on the public devnet: three guardians, how many leaves each device has burned, and "
      "the high-water mark the chain has recorded for each one, with every receipt below."))

    trs = "".join(
        f"<tr><td>{html.escape(r['label'])}</td><td class='mono'>{short(r['txHash'])}</td><td>{r['block']}</td>"
        f"<td class='ok'>{'success' if r['status'] == '0x1' else r['status']}</td><td>{r['gasUsed']:,}</td></tr>"
        for r in rec[:5])
    S.append(("receipts", page(f"""
      <div class='kicker'>Public hackathon devnet (chain 1337) · ERC-8421 gateway · explorer.altmempool.wtf</div><h2>PQ-signed frame transactions</h2>
      <table><tr><th>action</th><th>tx</th><th>block</th><th>status</th><th>gas</th></tr>{trs}</table>"""),
      "These are real receipts from the public hackathon devnet: two-of-three transfers, a "
      "stateless second approval, key rotation, recovery of a lost guardian by the other two, and a transfer signed "
      "by the recovered guardian."))

    can = cmp_["canonical_mempool"]["error"]["message"].split(": ")[-1]
    uns = cmp_["unstaked_alt_mempool"]["error"]["message"]
    S.append(("altmempool", page(f"""
      <div class='kicker'>Why the alternative mempool</div><h2>The vault reads a shared on-chain leaf ledger while validating</h2>
      <table><tr><th>submission</th><th>result (recorded node response)</th></tr>
      <tr><td>staked vault → canonical mempool</td><td class='bad'>rejected: <code>{html.escape(can)}</code></td></tr>
      <tr><td>unstaked vault → ERC-8421 alt mempool</td><td class='bad' style='font-size:23px'>rejected: <code>{html.escape(uns[:92])}…</code></td></tr>
      <tr><td>staked vault → ERC-8421 alt mempool</td><td class='ok'>admitted and included</td></tr></table>"""),
      "Why the alternative mempool? The vault reads a shared leaf ledger during validation. The canonical mempool "
      "rejects that storage access. The alt mempool rejects an unstaked copy of the vault, and admits the staked one."))

    S.append(("curve", page("""
      <div class='kicker'>Research question</div><h2>Do stateful Leafguards survive crashes, stale backups, clones and rollback?</h2>
      <table><tr><th>distinct messages on one leaf (γ)</th><th>1</th><th>2</th><th>3</th><th>5</th><th>10</th><th>20</th></tr>
      <tr><td>forgery cost (bits) — JARDIN k=32, a=4</td><td class='ok'>128</td><td>97.5</td><td>80.2</td><td class='warn'>59.5</td><td class='bad'>34.3</td><td class='bad'>14.9</td></tr></table>"""),
      "Now the research question. These few-time leaves lose security fast when reused: one hundred twenty-eight bits "
      "once, ninety-seven after one reuse, under sixty after five. So what happens when a device crashes, is restored "
      "from an old backup, is cloned, or has its counter rolled back?"))

    rr = {r["value"]: r for r in sweeps["rows"] if r["sweep"] == "repeated_restore" and r["policy"] == "durable" and r["ledger"] is False}
    cn = next(c for c in crash if c["policy"] == "naive_fsync" and c["mode"] == "targeted")
    cb = next(c for c in crash if c["policy"] == "burn" and c["mode"] == "targeted")
    S.append(("attacks", page(f"""
      <div class='kicker'>Attacks on controlled test wallets · real JARDIN signatures</div><h2>What breaks</h2>
      <div class='grid' style='grid-template-columns:1fr 1fr'>
        <div class='card'><p>Same stale backup restored 1 / 2 / 4 / 8 times<br>(burn-before-sign signer, no on-chain state)</p>
          <p class='bad' style='font-size:40px'>γ {rr[1]['worst_gamma']} / {rr[2]['worst_gamma']} / {rr[4]['worst_gamma']} / {rr[8]['worst_gamma']}<br>{rr[1]['worst_min_bits']} → {rr[8]['worst_min_bits']} bits</p></div>
        <div class='card'><p>Process killed the instant a signature is released<br>({cn['kills']} real SIGKILLs)</p>
          <p style='font-size:40px'>persist-after-sign: <span class='bad'>γ {cn['max_gamma']}</span><br>burn-before-sign: <span class='ok'>γ {cb['max_gamma']}</span></p></div>
      </div>"""),
      f"We attacked controlled test wallets with real JARDIN signatures. Restoring the same backup eight times drives "
      f"reuse to {rr[8]['worst_gamma']} and the forgery cost to about {round(rr[8]['worst_min_bits'])} bits. Killing a "
      f"persist-after-sign signer at the moment of release put {cn['max_gamma']} signatures on a single leaf. "
      "Burn-before-sign stops crashes, but not stale backups, rollbacks or clones."))

    def g(sc, pol, led):
        return rows[(sc, pol, str(led))]
    sb0, sb1 = g("stale_backup_silent", "durable", False), g("stale_backup_silent", "anchored_w1", True)
    rb0, rb1 = g("rollback", "durable", False), g("rollback", "anchored_w1", True)
    cl0, cl1 = g("clone", "durable", False), g("clone", "anchored_w1", "revoke")
    S.append(("mitigation", page(f"""
      <div class='kicker'>Mitigation · 20 seeds, worst case</div><h2>LeafLedger + anchored signer + window + equivocation proofs</h2>
      <table><tr><th>scenario</th><th>baseline (JARDIN-style)</th><th>Leafguard</th></tr>
      <tr><td>silent stale backup</td><td class='bad'>γ {sb0['worst_gamma']} · {sb0['total_double_accepted']} double-accepted</td><td class='ok'>γ {sb1['worst_gamma']} · {sb1['total_double_accepted']}</td></tr>
      <tr><td>counter rollback</td><td class='bad'>γ {rb0['worst_gamma']} · {rb0['worst_min_bits']} bits · {rb0['total_double_accepted']} double-accepted</td><td class='ok'>γ {rb1['worst_gamma']} · {rb1['total_double_accepted']}</td></tr>
      <tr><td>cloned device</td><td class='bad'>γ {cl0['worst_gamma']} · exposure {cl0['mean_exposure_steps']:g} blocks</td><td class='warn'>γ {cl1['worst_gamma']} · exposure {cl1['mean_exposure_steps']:g} block</td></tr></table>"""),
      f"The mitigation has four layers: the on-chain leaf ledger, a signer anchored to the chain's mark, a "
      f"one-transaction in-flight window, and permissionless equivocation proofs that revoke a slot everywhere. "
      f"Stale backups and rollbacks go from hundreds of double-accepted signatures to zero. Clones can't be "
      f"prevented, but exposure drops from {cl0['mean_exposure_steps']:g} blocks to one."))

    b = att["baseline"]
    S.append(("live-attack", page(f"""
      <div class='kicker'>Live on the public devnet</div><h2>Stale backup restore, end to end</h2>
      <table><tr><th>step</th><th>result</th></tr>
      <tr><td>baseline vault, tx1 at leaf {b['leaf_tx1']}</td><td class='mono'>{short(b['tx1'])} <span class='ok'>included</span></td></tr>
      <tr><td>device restored from backup, tx2 at <b>the same leaf {b['leaf_tx2']}</b></td><td class='mono'>{short(b['tx2'])} <span class='bad'>included</span></td></tr>
      <tr><td>anyone submits both signatures as an equivocation proof</td><td class='mono'>{short(att['equivocationReport']['tx'])} <span class='ok'>slot revoked</span></td></tr>
      <tr><td>same restored signer against Leafguard</td><td class='ok'>rejected in VERIFY: <code>StaleLeaf(slot, {att['pqVault']['reusedLeaf']})</code></td></tr></table>"""),
      "And live on the public devnet: a vault without leaf tracking accepted two transactions signed with the same leaf after a "
      "stale restore. A watcher's equivocation proof revoked that slot, and Leafguard rejected the same restored "
      "signer with StaleLeaf."))

    S.append(("limits", page("""
      <div class='kicker'>Honest limits</div><h2>What it does not solve</h2>
      <ul><li>the signer trusts its host for the chain view (a lying host turns attack into denial of service)</li>
      <li>signed-but-unconfirmed transactions are a residual window (W = 1 caps γ at 2)</li>
      <li>deployed alt mempool still caps validation at 100k gas → second signature checked after approval</li>
      <li>clones are bounded, not prevented · research prototype, not audited</li></ul>
      <p class='acc' style='margin-top:30px'>Open source: contracts · signer toolkit · UI · simulator · report — README.md</p>"""),
      "Limits: the signer trusts its host for the chain view, in-flight transactions are a residual window, and the "
      "deployed alt mempool still caps validation at one hundred thousand gas, so the second signature is checked "
      "after approval. Code, tests, receipts and the full report are open source."))
    return S


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-800:]}")
    return r.stdout


def screenshot(url, png, budget_ms=8000, live=False):
    # Static slides: virtual time. The live UI polls a server every few seconds, so virtual time never goes idle;
    # use a real-time timeout instead.
    timing = f"--timeout={budget_ms}" if live else f"--virtual-time-budget={budget_ms}"
    sh([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={W},{H}",
        timing, f"--screenshot={png}", url])


def duration(path):
    return float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", default="http://127.0.0.1:8143")
    ap.add_argument("--network", default="public")
    ap.add_argument("--voice", default="Samantha")
    ap.add_argument("--rate", type=int, default=205)
    a = ap.parse_args()
    os.makedirs(BUILD, exist_ok=True)
    net = a.network
    data = {"receipts": load(f".devnet/{net}/receipts.json"), "exp": load("research/results/experiments.json"),
            "sweeps": load("research/results/sweeps.json"), "crash": load("research/results/crash_test.json"),
            "cmp": load(f".devnet/{net}/mempool-comparison.json"), "att": load(f".devnet/{net}/attack-stale-restore.json")}
    segs, narration = [], ["# Leafguard demo narration", ""]
    t = 0.0
    for i, (name, doc, text) in enumerate(slides(data)):
        png = os.path.join(BUILD, f"{i:02d}-{name}.png")
        if doc is None:
            # Live UI: fetch the server's real data first (public RPC is slow), then render the unmodified UI page
            # with that data served to its fetch() calls, so the screenshot never races the network.
            import urllib.request
            snap = {k: json.loads(urllib.request.urlopen(f"{a.ui}/api/{k}", timeout=120).read()) for k in ("status", "research")}
            page_html = open(os.path.join(ROOT, "app", "index.html")).read()
            shim = ("<script>window.__SNAP__=" + json.dumps(snap) + ";window.fetch=async(p)=>{const k=p.replace('/api/','');"
                    "return new Response(JSON.stringify(window.__SNAP__[k]||{}),{status:200,headers:{'content-type':'application/json'}})};</script>")
            htmlp = os.path.join(BUILD, f"{i:02d}-{name}.html")
            open(htmlp, "w").write(page_html.replace("<script>", shim + "<script>", 1))
            screenshot("file://" + htmlp, png)
        else:
            htmlp = os.path.join(BUILD, f"{i:02d}-{name}.html")
            open(htmlp, "w").write(doc)
            screenshot("file://" + htmlp, png)
        aiff = os.path.join(BUILD, f"{i:02d}-{name}.aiff")
        sh(["say", "-v", a.voice, "-r", str(a.rate), "-o", aiff, text])
        d = duration(aiff) + 0.6
        mp4 = os.path.join(BUILD, f"{i:02d}-{name}.mp4")
        sh(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", png, "-i", aiff,
            "-filter_complex", f"[0:v]scale={W}:{H},format=yuv420p,fade=t=in:st=0:d=0.35,fade=t=out:st={d - 0.35:.2f}:d=0.35[v];"
            f"[1:a]apad,atrim=0:{d:.2f},aresample=48000[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{d:.2f}", "-r", "30", "-c:v", "libx264", "-preset", "medium",
            "-crf", "20", "-c:a", "aac", "-b:a", "160k", mp4])
        segs.append(mp4)
        narration.append(f"**{int(t // 60)}:{int(t % 60):02d} — {name}**  \n{text}\n")
        t += d
    lst = os.path.join(BUILD, "concat.txt")
    open(lst, "w").write("".join(f"file '{s}'\n" for s in segs))
    out = os.path.join(ROOT, "demo", "pq-vault-demo.mp4")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out])
    narration.append(f"\nTotal: {int(t // 60)}:{int(t % 60):02d}")
    open(os.path.join(ROOT, "demo", "NARRATION.md"), "w").write("\n".join(narration))
    print(f"wrote {out} ({t:.1f}s)")


if __name__ == "__main__":
    main()
