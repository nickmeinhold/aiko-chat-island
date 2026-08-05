"""Additive-only migration lint — the structural half of task #11 (A1).

WHY (task #11 crucible, `docs/crucible/11-expand-contract-migrations/`): the islands
roll back by re-pinning an older image onto the same volume, so the previous code must
tolerate the newer schema (N-1 / cumulative compatibility). That is FREE for purely
*additive* migrations and broken ONLY by a *contracting* one. This lint keeps the
migration stream additive: it AST-walks each migration's ``upgrade()`` and FAILs on any
op that breaks backward compatibility, so a contract can only land through the explicit,
labelled escape hatch (a two-/three-release expand-contract split).

SCOPE, honestly (v2 re-Temper): this proves the *structural* guarantee only. It CANNOT
see semantics — a kept-but-repurposed column, a data migration that re-keys values, a
``server_default`` that a security column reads wrong. Those are flagged REVIEW (human
sign-off) or left to a watched deploy; they are NOT auto-proven safe here. Do not read a
green lint as "rollback is safe" — read it as "no structural contracting op shipped."

Pure stdlib (``ast`` only) so it honours the CI test-isolation invariant (no
aiko_services / no DB). Analyses ``upgrade()`` ONLY — prod never runs ``downgrade()``
(the schema ratchets forward; see aiko_gateway.migrate).
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass

# Severities.
CONTRACTING = "contracting"  # hard FAIL — breaks N-1 backward compatibility
REVIEW = "review"            # needs human sign-off; semantics the lint cannot prove safe

# Escape hatch: a migration that legitimately contracts (the contract phase of an
# expand/contract split) carries this marker; it downgrades a hard FAIL to REVIEW (the
# PR still needs the `migration-contract` label — the human gate). Full attestation
# verification (booting the stop-use image) is A2, not this lint.
ANNOTATION = "expand-contract: contract-phase"


@dataclass(frozen=True)
class Finding:
    severity: str
    op: str
    table: str | None
    reason: str

    def __str__(self) -> str:
        loc = f" on {self.table!r}" if self.table else ""
        return f"[{self.severity}] {self.op}{loc}: {self.reason}"


def analyze_source(source: str) -> list[Finding]:
    """Classify the ops in a migration's ``upgrade()``. Returns findings (possibly empty).

    A CONTRACTING finding means the migration is not backward-compatible and must not
    ship in one release without the expand-contract annotation. REVIEW means the lint
    cannot prove it safe and a human must sign the `migration-contract` label.
    """
    tree = ast.parse(source)
    upgrade = _find_func(tree, "upgrade")
    if upgrade is None:
        return []
    created = _created_tables(upgrade)
    findings: list[Finding] = []
    for method, call, table, is_batch in _iter_ops(upgrade):
        # New-table carve-out (Wu re-Temper): an op on a table CREATE'd in this same
        # migration is safe — no deployed code ever wrote that table. Without this the
        # hardening rows fire on the commonest shape (add a feature table) and train
        # rubber-stamping. create_table itself is always safe.
        if table is not None and table in created:
            continue
        f = _classify(method, call, table, is_batch)
        if f is not None:
            findings.append(f)

    if ANNOTATION in source:
        # Annotated contract phase: downgrade hard FAILs to REVIEW (still label-gated).
        findings = [
            Finding(REVIEW if f.severity == CONTRACTING else f.severity, f.op, f.table,
                    f.reason + " [contract-phase annotated — needs migration-contract label]")
            for f in findings
        ]
    return findings


# ---- AST helpers ---------------------------------------------------------------

def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _str_arg(node: ast.expr | None) -> str | None:
    """A string-literal arg's value, else None (a variable/expr → unknown table)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _created_tables(func: ast.FunctionDef) -> set[str]:
    created: set[str] = set()
    for method, call, _table, _is_batch in _iter_ops(func):
        if method == "create_table" and call.args:
            name = _str_arg(call.args[0])
            if name:
                created.add(name)
    return created


def _iter_ops(func: ast.FunctionDef):
    """Yield (method, call_node, table, is_batch) for every ``op.*`` / ``batch_op.*``
    call in ``upgrade()``, tracking the table bound by ``op.batch_alter_table``.

    Handles nested batch blocks and a non-standard ``as`` name. A batch op's table is
    the batch table; a direct op's table is extracted per-method by the caller-less
    ``_op_table`` below."""
    # Recurse the statement tree, maintaining a stack of (batch_var_name, batch_table)
    # for the ``op.batch_alter_table(...) as b:`` context we're inside.
    def walk_body(stmts, batch_stack):
        for stmt in stmts:
            if isinstance(stmt, ast.With):
                batch = _batch_context(stmt)
                if batch is not None:
                    # Classify the batch_alter_table call itself (recreate-via-args can
                    # contract without an explicit inner drop), THEN its body.
                    batch_call = _batch_call(stmt)
                    if batch_call is not None:
                        yield "batch_alter_table", batch_call, batch[1], False
                    yield from walk_body(stmt.body, batch_stack + [batch])
                    continue
                yield from walk_body(stmt.body, batch_stack)
                continue
            # Any other compound statement (if/for/try) — descend into its bodies.
            inner = _child_stmt_lists(stmt)
            if inner:
                for body in inner:
                    yield from walk_body(body, batch_stack)
            # Classify calls appearing directly in this statement.
            for call in _direct_calls(stmt):
                m = _method_of(call)
                if m is None:
                    continue
                owner = _owner_of(call)
                if owner == "op":
                    yield m, call, _op_table(m, call), False
                elif batch_stack and owner == batch_stack[-1][0]:
                    yield m, call, batch_stack[-1][1], True

    yield from walk_body(func.body, [])


def _child_stmt_lists(stmt: ast.stmt) -> list[list[ast.stmt]]:
    lists: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        val = getattr(stmt, field, None)
        if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
            lists.append(val)
    for handler in getattr(stmt, "handlers", []) or []:
        if getattr(handler, "body", None):
            lists.append(handler.body)
    return lists


def _direct_calls(node: ast.AST):
    """All Call nodes anywhere within a single statement's expression tree."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            yield n


def _batch_call(with_node: ast.With) -> ast.Call | None:
    """The ``op.batch_alter_table(...)`` Call node of a batch ``with`` block."""
    for item in with_node.items:
        call = item.context_expr
        if (isinstance(call, ast.Call) and _method_of(call) == "batch_alter_table"
                and _owner_of(call) == "op"):
            return call
    return None


def _batch_context(with_node: ast.With) -> tuple[str, str | None] | None:
    """If this ``with`` is ``op.batch_alter_table("t") as b:`` return (b, "t")."""
    for item in with_node.items:
        call = item.context_expr
        if (isinstance(call, ast.Call) and _method_of(call) == "batch_alter_table"
                and _owner_of(call) == "op"):
            var = item.optional_vars
            name = var.id if isinstance(var, ast.Name) else "batch_op"
            table = _str_arg(call.args[0]) if call.args else None
            return name, table
    return None


def _method_of(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _owner_of(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.value.id
    return None


def _op_table(method: str, call: ast.Call) -> str | None:
    """The target table for a direct ``op.<method>`` call (for the new-table carve-out)."""
    a = call.args
    if method in ("add_column", "drop_column", "alter_column", "drop_table", "create_table"):
        return _str_arg(a[0]) if a else None
    if method in ("create_index", "create_foreign_key", "create_unique_constraint",
                  "create_check_constraint", "drop_constraint", "drop_index"):
        # (name, table, ...) — table is the SECOND positional for these.
        return _str_arg(a[1]) if len(a) > 1 else None
    if method == "rename_table":
        return _str_arg(a[0]) if a else None
    return None


# ---- classification ------------------------------------------------------------

def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _find_column_call(call: ast.Call, is_batch: bool) -> ast.Call | None:
    """The ``sa.Column(...)`` arg of an add_column call.

    direct: op.add_column("t", sa.Column(...))  → args[1]
    batch:  batch_op.add_column(sa.Column(...)) → args[0]
    """
    idx = 0 if is_batch else 1
    if len(call.args) > idx and isinstance(call.args[idx], ast.Call):
        c = call.args[idx]
        if _method_of(c) == "Column":
            return c
    return None


def _column_flags(col: ast.Call) -> tuple[bool, bool, bool]:
    """(not_null, has_server_default, unique) for a Column(...) call."""
    kw = _kwargs(col)
    not_null = _is_false(kw.get("nullable"))
    has_default = "server_default" in kw and not _is_none(kw["server_default"])
    unique = _is_true(kw.get("unique"))
    return not_null, has_default, unique


def _is_none(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _classify(method: str, call: ast.Call, table: str | None, is_batch: bool) -> Finding | None:
    kw = _kwargs(call)

    if method in ("drop_column",):
        return Finding(CONTRACTING, method, table, "drops a column old code may read")
    if method == "drop_table":
        return Finding(CONTRACTING, method, table, "drops a table old code may query")
    if method == "rename_table":
        return Finding(CONTRACTING, method, table, "renames a table old code references")
    if method == "drop_constraint":
        return Finding(CONTRACTING, method, table,
                       "drops a constraint (UNIQUE→dup rows break .one(); FK→loses "
                       "ON DELETE CASCADE; CHECK→admits values old readers assume impossible)")
    if method == "create_foreign_key":
        return Finding(CONTRACTING, method, table,
                       "adds a FK; an old write inserting an orphan now raises IntegrityError")
    if method == "create_check_constraint":
        return Finding(CONTRACTING, method, table,
                       "tightens with a CHECK; old writes may violate it")
    if method == "create_unique_constraint":
        return Finding(CONTRACTING, method, table,
                       "tightens with UNIQUE; old writes may collide")
    if method == "create_index":
        if _is_true(kw.get("unique")):
            return Finding(CONTRACTING, method, table,
                           "unique index IS a constraint tighten; old writes may hit IntegrityError")
        return None  # plain index — safe
    if method == "drop_index":
        return None  # a plain index drop is relaxing; a unique constraint is dropped via drop_constraint

    if method == "add_column":
        col = _find_column_call(call, is_batch)
        if col is None:
            return Finding(REVIEW, method, table, "add_column whose Column(...) could not be parsed")
        not_null, has_default, unique = _column_flags(col)
        if unique:
            return Finding(CONTRACTING, method, table, "add unique column; old writes may collide")
        if not_null and not has_default:
            return Finding(CONTRACTING, method, table,
                           "add NOT NULL column without server_default; old INSERT violates it")
        if has_default:
            return Finding(REVIEW, method, table,
                           "server_default: old INSERTs omit the column and get the default — "
                           "healthy-but-wrong if a security/authz/billing column reads it (verify inert)")
        return None  # nullable additive column — safe

    if method == "alter_column":
        if "new_column_name" in kw:
            return Finding(CONTRACTING, method, table, "renames a column old code references")
        if "type_" in kw:
            return Finding(CONTRACTING, method, table,
                           "changes column type; narrowing truncates and on SQLite even a widen "
                           "rides a table-recreate that changes affinity/rounding")
        if _is_false(kw.get("nullable")) and not ("server_default" in kw and not _is_none(kw["server_default"])):
            return Finding(CONTRACTING, method, table,
                           "tightens to NOT NULL without server_default; old INSERT/rows violate it")
        if "server_default" in kw and not _is_none(kw["server_default"]):
            return Finding(REVIEW, method, table, "sets a server_default (verify semantically inert)")
        return None

    if method == "execute":
        return Finding(REVIEW, method, table,
                       "raw SQL — semantics unverifiable statically (a backfill and a semantic "
                       "re-key look identical here); the A2 runtime smoke is its real gate")

    if method == "batch_alter_table":
        # A recreate expressed via arguments (copy_from / reduced reflected columns /
        # recreate=) can contract without an explicit batch_op.drop_*; flag for review.
        if any(k in kw for k in ("copy_from", "recreate", "reflect_args", "table_args")):
            return Finding(REVIEW, method, table,
                           "batch recreate via arguments can drop/retype columns and loses "
                           "triggers/dependents; verify it is additive")
        return None

    return None


# ---- CLI ------------------------------------------------------------------------

def lint_files(paths: list[str]) -> dict[str, list[Finding]]:
    out: dict[str, list[Finding]] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            findings = analyze_source(fh.read())
        if findings:
            out[p] = findings
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: migration_lint.py <migration.py> [<migration.py> ...]", file=sys.stderr)
        return 2
    results = lint_files(argv)
    contracting = 0
    for path, findings in results.items():
        print(f"\n{path}:")
        for f in findings:
            print(f"  {f}")
            if f.severity == CONTRACTING:
                contracting += 1
    if contracting:
        print(f"\nFAIL: {contracting} contracting op(s) — not backward-compatible. Split "
              f"across releases (expand/contract) or annotate '{ANNOTATION}' + apply the "
              f"migration-contract label.", file=sys.stderr)
        return 1
    review = sum(1 for fs in results.values() for f in fs if f.severity == REVIEW)
    if review:
        print(f"\nREVIEW: {review} op(s) the lint cannot prove safe — needs human sign-off.")
    print("\nOK: no structural contracting op." if not results else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
