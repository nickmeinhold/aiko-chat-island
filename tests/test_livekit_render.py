"""The bundled-SFU config renderer (`deploy/livekit/render-config.sh`).

The bundled arm keeps `livekit.yaml` OUT of git — it carries the API secret — and
renders it from a repo-authoritative template plus a per-box `.env`. That makes the
renderer the thing standing between "the template is right" and "the box is right",
so it is tested directly rather than only in situ, exactly as
`test_deploy_preflight.py` does for the APNs preflight.

BOTH CONTROLS ARE BUILT HERE. Every failure mode of a half-rendered LiveKit config
is SILENT at boot: LiveKit parses a config with a blank `node_ip` quite happily, then
advertises an unreachable ICE candidate, and every call fails at CONNECT rather than
at start-up. So the arms that must go RED matter more than the one that must go
green — a renderer that emits something plausible for bad input is worse than one
that emits nothing.

The green arm is a real fidelity check, not a smoke test: the rendered output is
compared, parsed, against the config a live island actually runs. A template that
renders without error but drops `deny_peer_cidrs` would pass a smoke test and
reopen the CGNAT SSRF hole (task #6).
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "livekit" / "render-config.sh"
TEMPLATE = ROOT / "deploy" / "livekit" / "livekit.yaml.template"

COMPLETE = {
    "LIVEKIT_NODE_IP": "203.0.113.9",          # TEST-NET-3, never routable
    "LIVEKIT_TURN_DOMAIN": "turn.example.org",
    "LIVEKIT_API_KEY_ID": "APIexample0000",
    "LIVEKIT_API_SECRET": "c2VjcmV0LWZvci10ZXN0cy1vbmx5",
}


def _prepare(tmp_path: Path, env: dict[str, str] | None, *, template: str | None = None) -> Path:
    """Throwaway copy of deploy/livekit/ — the renderer never runs against the repo."""
    d = tmp_path / "livekit"
    d.mkdir()
    (d / "livekit.yaml.template").write_text(
        template if template is not None else TEMPLATE.read_text()
    )
    (d / "render-config.sh").write_bytes(SCRIPT.read_bytes())
    (d / "render-config.sh").chmod(0o755)
    if env is not None:
        (d / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    return d


def _render(tmp_path: Path, env: dict[str, str] | None, *, template: str | None = None):
    d = _prepare(tmp_path, env, template=template)
    return subprocess.run(
        [str(d / "render-config.sh"), "--check"],
        capture_output=True, text=True,
    )


# --- arms that MUST go red -------------------------------------------------

def test_missing_env_file_refuses(tmp_path):
    r = _render(tmp_path, None)
    assert r.returncode != 0
    assert "no" in r.stderr.lower() and ".env" in r.stderr


@pytest.mark.parametrize("blanked", sorted(COMPLETE))
def test_any_blank_required_value_refuses(tmp_path, blanked):
    """Each of the four independently. A blank node_ip and a blank API secret fail
    in completely different ways at runtime, and neither fails at boot."""
    env = dict(COMPLETE, **{blanked: ""})
    r = _render(tmp_path, env)
    assert r.returncode != 0, f"rendered anyway with {blanked} blank"
    assert blanked in r.stderr


@pytest.mark.parametrize("dropped", sorted(COMPLETE))
def test_any_missing_required_key_refuses(tmp_path, dropped):
    env = {k: v for k, v in COMPLETE.items() if k != dropped}
    r = _render(tmp_path, env)
    assert r.returncode != 0, f"rendered anyway with {dropped} absent"
    assert dropped in r.stderr


def test_unknown_placeholder_refuses(tmp_path):
    """A ${VAR} the renderer does not know about must abort, never silently empty.
    envsubst's default mode would substitute it with "" and produce a config that
    parses."""
    r = _render(tmp_path, COMPLETE, template="port: 7880\nbogus: ${LIVEKIT_NOT_DECLARED}\n")
    assert r.returncode != 0
    assert "unsubstituted" in r.stderr.lower()


def test_placeholder_inside_a_comment_does_not_block(tmp_path):
    """The guard must look at the CONFIG, not the documentation. The real template
    explains its own ${VAR} mechanism in prose, and a whole-file scan fires on that
    — it did, on this renderer's first run. A check a comment can trip is a check
    people learn to route around."""
    tpl = "# explains ${LIVEKIT_NOT_DECLARED} in prose\nport: 7880\n"
    r = _render(tmp_path, COMPLETE, template=tpl)
    assert r.returncode == 0, r.stderr
    assert yaml.safe_load(r.stdout) == {"port": 7880}


# --- arms that must stay green --------------------------------------------

def test_renders_and_substitutes_every_value(tmp_path):
    r = _render(tmp_path, COMPLETE)
    assert r.returncode == 0, r.stderr
    cfg = yaml.safe_load(r.stdout)
    assert cfg["rtc"]["node_ip"] == COMPLETE["LIVEKIT_NODE_IP"]
    assert cfg["turn"]["domain"] == COMPLETE["LIVEKIT_TURN_DOMAIN"]
    # The key ID is substituted and visible; the SECRET's value is asserted on the
    # written file below, because --check deliberately redacts it.
    assert list(cfg["keys"]) == [COMPLETE["LIVEKIT_API_KEY_ID"]]


def test_check_mode_redacts_the_secret(tmp_path):
    """--check answers "does this render?", never "show me the credential" — its
    output goes to terminals, pipes and CI logs (cage-match PR#151, Carnot)."""
    r = _render(tmp_path, COMPLETE)
    assert COMPLETE["LIVEKIT_API_SECRET"] not in r.stdout
    assert yaml.safe_load(r.stdout)["keys"][COMPLETE["LIVEKIT_API_KEY_ID"]] == "<redacted>"


def test_written_file_carries_the_real_secret(tmp_path):
    """Redaction is a --check-only affordance. The file the SFU actually reads must
    carry the real value, or the renderer would ship a config that authenticates
    nothing — a redaction that leaked into the write path would be far worse than
    the leak it prevents."""
    d = _prepare(tmp_path, COMPLETE)
    subprocess.run([str(d / "render-config.sh")], capture_output=True, text=True, check=True)
    cfg = yaml.safe_load((d / "livekit.yaml").read_text())
    assert cfg["keys"] == {COMPLETE["LIVEKIT_API_KEY_ID"]: COMPLETE["LIVEKIT_API_SECRET"]}


def test_ssrf_hardening_survives_rendering(tmp_path):
    """`deny_peer_cidrs` is the CGNAT SSRF fix (task #6). A TURN server is an open
    proxy by design; losing this line is a silent regression that no smoke test and
    no successful call would ever surface."""
    cfg = yaml.safe_load(_render(tmp_path, COMPLETE).stdout)
    assert "100.64.0.0/10" in cfg["turn"]["deny_peer_cidrs"]


def test_matches_the_shape_a_live_island_runs(tmp_path):
    """Fidelity, not smoke. These are the fields a live island's SFU actually sets;
    a template that renders cleanly but drops one of them is a config that boots and
    then behaves wrongly."""
    cfg = yaml.safe_load(_render(tmp_path, COMPLETE).stdout)
    assert cfg["port"] == 7880
    assert cfg["rtc"]["tcp_port"] == 7881
    assert (cfg["rtc"]["port_range_start"], cfg["rtc"]["port_range_end"]) == (7882, 7892)
    assert cfg["turn"]["enabled"] is True
    assert cfg["turn"]["external_tls"] is True
    assert (cfg["turn"]["relay_range_start"], cfg["turn"]["relay_range_end"]) == (50000, 60000)


def test_secret_is_not_world_readable_when_written(tmp_path):
    """--check writes nothing; the real path writes mode 600 because the file holds
    the API secret."""
    d = _prepare(tmp_path, COMPLETE)
    subprocess.run([str(d / "render-config.sh")], capture_output=True, text=True, check=True)
    assert (d / "livekit.yaml").stat().st_mode & 0o077 == 0
