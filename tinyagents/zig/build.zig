const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const sdk = b.addModule("tinyagent", .{
        .root_source_file = b.path("src/root.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });
    // Only schema validation crosses this native boundary; the agent and its
    // providers, handlers, sessions, and transports remain Zig code.
    const rust_cpu = switch (target.result.cpu.arch) {
        .aarch64 => "aarch64",
        .x86_64 => "x86_64",
        else => @panic("The schema library currently supports aarch64 and x86_64"),
    };
    const rust_os = switch (target.result.os.tag) {
        .macos => "apple-darwin",
        .linux => if (target.result.abi.isMusl()) "unknown-linux-musl" else "unknown-linux-gnu",
        .windows => if (target.result.cpu.arch == .aarch64) "pc-windows-gnullvm" else "pc-windows-gnu",
        else => @panic("The schema library currently supports macOS, Linux, and Windows GNU"),
    };
    const rust_target = b.fmt("{s}-{s}", .{ rust_cpu, rust_os });
    const schema = b.addSystemCommand(&.{ "cargo", "build", "--quiet", "--release", "--locked", "--target", rust_target, "--manifest-path" });
    schema.addFileArg(b.path("schema-native/Cargo.toml"));
    schema.addFileInput(b.path("schema-native/Cargo.lock"));
    schema.addFileInput(b.path("schema-native/src/lib.rs"));
    schema.addArg("--target-dir");
    const schema_output = schema.addOutputDirectoryArg("schema");
    sdk.addObjectFile(schema_output.path(b, b.fmt("{s}/release/libtinyagent_schema.a", .{rust_target})));
    const test_step = b.step("test", "Run SDK checks");
    const cli = b.addExecutable(.{
        .name = "tinyagent",
        .root_module = b.createModule(.{
            .root_source_file = b.path("tiny_agent.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    b.installArtifact(cli);
    const run_cli = b.addRunArtifact(cli);
    if (b.args) |args| run_cli.addArgs(args);
    b.step("run", "Run the coding CLI").dependOn(&run_cli.step);
    const self_test = b.addRunArtifact(cli);
    self_test.addArg("--self-test");
    test_step.dependOn(&self_test.step);
    const tool_step = b.step("test-tools", "Run checked native tool tests");
    const bash_step = b.step("test-bash", "Run guarded Bash process checks");
    const cli_step = b.step("test-cli", "Run coding CLI checks");
    cli_step.dependOn(&self_test.step);
    const console_fixture = b.addExecutable(.{
        .name = "tinyagent-console-fixture",
        .root_module = b.createModule(.{
            .root_source_file = b.path("test/console_fixture.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    const cli_tests = b.addSystemCommand(&.{ "python3", "-I", "test/cli.py" });
    cli_tests.addArtifactArg(cli);
    cli_tests.addArtifactArg(console_fixture);
    test_step.dependOn(&cli_tests.step);
    cli_step.dependOn(&cli_tests.step);
    for ([_][]const u8{ "test/core.zig", "test/providers.zig", "test/sessions.zig", "test/tools.zig", "test/tool_loop.zig", "test/bash.zig", "test/console.zig" }) |path| {
        const tests = b.addTest(.{
            .root_module = b.createModule(.{
                .root_source_file = b.path(path),
                .target = target,
                .optimize = optimize,
                .imports = &.{.{ .name = "tinyagent", .module = sdk }},
            }),
        });
        const run = b.addRunArtifact(tests);
        test_step.dependOn(&run.step);
        if (std.mem.eql(u8, path, "test/tools.zig") or std.mem.eql(u8, path, "test/tool_loop.zig")) tool_step.dependOn(&run.step);
        if (std.mem.eql(u8, path, "test/bash.zig")) bash_step.dependOn(&run.step);
        if (std.mem.eql(u8, path, "test/console.zig")) cli_step.dependOn(&run.step);
    }

    const example = b.addExecutable(.{
        .name = "tinyagent-offline",
        .root_module = b.createModule(.{
            .root_source_file = b.path("examples/offline.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    b.installArtifact(example);
    b.step("example", "Run the offline SDK example").dependOn(&b.addRunArtifact(example).step);

    const tools_example = b.addExecutable(.{
        .name = "tinyagent-tools-example",
        .root_module = b.createModule(.{
            .root_source_file = b.path("examples/tools.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    b.installArtifact(tools_example);
    const run_tools = b.addRunArtifact(tools_example);
    test_step.dependOn(&run_tools.step);
    b.step("example-tools", "Run the offline tool example").dependOn(&run_tools.step);

    const rpc = b.addExecutable(.{
        .name = "tinyagent-rpc-example",
        .root_module = b.createModule(.{
            .root_source_file = b.path("examples/rpc.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    b.installArtifact(rpc);

    const http = b.addExecutable(.{
        .name = "tinyagent-http-example",
        .root_module = b.createModule(.{
            .root_source_file = b.path("examples/http.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    b.installArtifact(http);

    const rpc_fixture = b.addExecutable(.{
        .name = "tinyagent-rpc-fixture",
        .root_module = b.createModule(.{
            .root_source_file = b.path("test/rpc_fixture.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    const rpc_tests = b.addSystemCommand(&.{ "python3", "test/rpc.py" });
    rpc_tests.addArtifactArg(rpc_fixture);
    test_step.dependOn(&rpc_tests.step);

    const http_fixture = b.addExecutable(.{
        .name = "tinyagent-http-fixture",
        .root_module = b.createModule(.{
            .root_source_file = b.path("test/http_fixture.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "tinyagent", .module = sdk }},
        }),
    });
    const http_tests = b.addSystemCommand(&.{ "python3", "test/http.py" });
    http_tests.addArtifactArg(http_fixture);
    test_step.dependOn(&http_tests.step);
    b.step("test-http", "Run native HTTP socket checks").dependOn(&http_tests.step);

    const output_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("test/output.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "rpc_output", .module = b.createModule(.{
                .root_source_file = b.path("src/rpc_output.zig"),
                .target = target,
                .optimize = optimize,
            }) }},
        }),
    });
    test_step.dependOn(&b.addRunArtifact(output_tests).step);
}
