/// `island`: the operator's tool ON the island box, shipped as a prebuilt
/// executable (CI compiles it; the box runs a binary and needs no Dart). ISL-0003
/// keeps toolchains off the box, not executables (amended 2026-10-25).
///
///   island standup ...    stand up a NEW island on a box that has none
///   island configure ...  change a live island's settings (not built yet)
///
/// Run from the island checkout: the directory holding docker-compose.yml.
library;

import 'dart:io';

import 'package:island_deploy/standup.dart';

const _help = '''
island: operator tool for an aiko island box

  island standup --domain <host> --name "<label>" [options]
      Stand up a NEW island. Refuses if this box already has one.
  island configure
      Change a live island's settings (not built yet: until it is, re-run
      deploy/standup.sh with the flag to change).

Run `island standup --help` for standup's options.''';

Future<void> main(List<String> args) async {
  if (args.isEmpty || args.first == '-h' || args.first == '--help') {
    stdout.writeln(_help);
    exit(args.isEmpty ? 64 : 0);
  }
  switch (args.first) {
    case 'standup':
      final rest = args.sublist(1);
      if (rest.contains('-h') || rest.contains('--help')) {
        stdout.writeln(usage);
        exit(0);
      }
      try {
        final opts = parseStandupArgs(rest);
        final result = await Standup(
          runner: const ProcessRunner(),
          root: Directory.current,
          say: Say(stdout.writeln, stderr.writeln),
          environment: Platform.environment,
          probe: _probe,
        ).run(opts);
        // 3, not 0: an island that never answered must not read as success to a script.
        exit(switch (result) {
          Healthy() => 0,
          StartedNotAnswering() => 3,
        });
      } on Refusal catch (e) {
        stderr.writeln(' fail ${e.message}');
        exit(1);
      }
    case 'configure':
      stderr.writeln(
        'island configure is not built yet. Until it is, change a live '
        "island's settings by re-running deploy/standup.sh with the flag to change.",
      );
      exit(2);
    default:
      stderr.writeln('unknown command: ${args.first}\n\n$_help');
      exit(64);
  }
}

/// Whether [uri] answers 2xx within a few seconds. Any failure is "not yet".
Future<bool> _probe(Uri uri) async {
  final client = HttpClient()..connectionTimeout = const Duration(seconds: 3);
  try {
    final req = await client.getUrl(uri).timeout(const Duration(seconds: 4));
    final res = await req.close().timeout(const Duration(seconds: 4));
    await res.drain<void>();
    return res.statusCode >= 200 && res.statusCode < 300;
  } on Object {
    return false;
  } finally {
    client.close(force: true);
  }
}
