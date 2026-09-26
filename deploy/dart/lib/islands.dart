/// The island registry, and the two processes this tool is allowed to spawn.
///
/// READ-ONLY BY CONSTRUCTION. Nothing here writes to a box. That is not a
/// discipline this file asks the reader to maintain — it is the entire command
/// surface, and both commands are reads (`sops -d`, `ssh … cat`). A design that
/// SHIPPED `.env` to the box went four `/design-temper` rounds and never reached
/// SOUND (`docs/design/16-TEMPER.md`); every finding across those rounds was
/// about WRITING or about the generation directory writing required. None of
/// them survive a tool that cannot write.
library;

import 'dart:convert';
import 'dart:io';

/// One island the operator can check.
class Island {
  const Island(this.name, {required this.sshHost, required this.remotePath});
  final String name;
  final String sshHost;
  final String remotePath;
}

/// The two live islands. Kept in code rather than in a conf file the tool would
/// have to parse: there is no `deploy/islands/` in this repo, and inventing one
/// to hold two rows is the kind of machinery four temper rounds spent their time
/// deleting. A third island adds a row.
const islands = <String, Island>{
  'enspyr': Island(
    'enspyr',
    sshHost: 'nick-mel',
    remotePath: '~/apps/aiko-chat-gateway',
  ),
  'imagineering': Island(
    'imagineering',
    sshHost: 'imagineering',
    remotePath: '~/apps/aiko-chat-gateway',
  ),
};

/// A spawn that either produced stdout or explains why it could not.
///
/// Sealed for the same reason the outcome type is: a caller that forgets the
/// failure arm does not compile. The Python version returned a
/// `CompletedProcess` and relied on every call site remembering to check
/// `returncode` — three call sites, three chances.
sealed class Read {
  const Read();
}

final class ReadOk extends Read {
  const ReadOk(this.text);
  final String text;
}

final class ReadFailed extends Read {
  const ReadFailed(this.reason);
  final String reason;
}

/// How long either spawn may take before it is a failure rather than a wait.
const _timeout = Duration(seconds: 60);

/// `sops -d` over a pipe.
///
/// The plaintext never touches a file, argv or the environment — the three
/// exfiltration surfaces the v1 temper named (`/proc/<pid>/environ` is readable,
/// `set -x` echoes, children inherit, and a trap does not run on SIGKILL).
/// Removing the file removes the shredding problem rather than solving it.
Future<Read> decryptRepoEnv(String repoRoot, String island) async {
  final path = '$repoRoot/deploy/secrets/$island.env.sops';
  if (!File(path).existsSync()) {
    return ReadFailed('no encrypted config at $path');
  }
  return _run(
    'sops',
    ['-d', path],
    notInstalled: 'sops is not installed on this machine (control-side only)',
    failedPrefix: 'sops could not decrypt $island.env.sops',
  );
}

/// Read the box's `.env`. READ ONLY — the only thing in this tool that touches
/// a box, and `cat` is the whole of it.
///
/// `BatchMode=yes` so a box that would prompt for a passphrase FAILS instead of
/// hanging on a tty that may not be attached. A hang is the worst of the three
/// outcomes because it is the one an operator reads as "still working".
Future<Read> readLiveEnv(Island island) async => _run(
  'ssh',
  ['-o', 'BatchMode=yes', island.sshHost, 'cat ${island.remotePath}/.env'],
  notInstalled: 'ssh is not on PATH',
  failedPrefix: 'could not read ${island.remotePath}/.env on ${island.sshHost}',
);

Future<Read> _run(
  String exe,
  List<String> args, {
  required String notInstalled,
  required String failedPrefix,
}) async {
  final ProcessResult result;
  try {
    result = await Process.run(
      exe,
      args,
      stdoutEncoding: utf8,
      stderrEncoding: utf8,
    ).timeout(_timeout);
  } on ProcessException {
    return ReadFailed(notInstalled);
  } on Object catch (e) {
    // Includes TimeoutException. A timeout is COULD-NOT-RUN, never a match:
    // "a check that could not run is not a check that passed".
    return ReadFailed('$failedPrefix: ${e.runtimeType}');
  }
  if (result.exitCode != 0) {
    final err = (result.stderr as String).trim();
    return ReadFailed(
      '$failedPrefix: ${err.length > 400 ? err.substring(0, 400) : err}',
    );
  }
  return ReadOk(result.stdout as String);
}
