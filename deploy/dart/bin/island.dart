/// `island`: the operator's tool ON the island box, shipped as a prebuilt
/// executable (CI compiles it; the box runs a binary and needs no Dart). ISL-0003
/// keeps toolchains off the box, not executables (amended 2026-10-25).
///
///   island standup ...    stand up a NEW island on a box that has none
///   island configure ...  change a live island's settings, editing its .env
///
/// Run from the island checkout: the directory holding docker-compose.yml.
library;

import 'dart:io';

import 'package:island_deploy/configure.dart';
import 'package:island_deploy/standup.dart';

const _help = '''
island: operator tool for an aiko island box

  island standup --domain <host> --name "<label>" [options]
      Stand up a NEW island. Refuses if this box already has one.
  island configure [--name ...] [--seed-peers ...] [--enable-passkeys | --no-passkeys]
      Change a live island's settings. With no options, print them.

Run `island standup --help` or `island configure --help` for the options.''';

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
          probe: probeHealth,
        ).run(opts);
        // 0 only for "up and confirmed"; the mapping lives in the library, tested.
        exit(exitCodeFor(result));
      } on Refusal catch (e) {
        stderr.writeln(' fail ${e.message}');
        exit(1);
      } on Object catch (e) {
        // Anything before the birth (after it, run() reports what it created).
        stderr.writeln(' fail unexpected error, nothing reported as done: $e');
        exit(1);
      }
    case 'configure':
      final rest = args.sublist(1);
      if (rest.contains('-h') || rest.contains('--help')) {
        stdout.writeln(configureUsage);
        exit(0);
      }
      try {
        final opts = parseConfigureArgs(rest);
        final configure = Configure(
          runner: const ProcessRunner(),
          root: Directory.current,
          say: Say(stdout.writeln, stderr.writeln),
          fetch: (u) => getLocal(u, maxBytes: 64 * 1024),
        );
        if (opts.isEmpty) {
          configure.show();
          exit(0);
        }
        exit(configureExitCode(await configure.run(opts)));
      } on Refusal catch (e) {
        stderr.writeln(' fail ${e.message}');
        exit(1);
      } on Object catch (e) {
        stderr.writeln(' fail unexpected error: $e');
        exit(1);
      }
    default:
      stderr.writeln('unknown command: ${args.first}\n\n$_help');
      exit(64);
  }
}
