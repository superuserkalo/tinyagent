namespace TinyAgent.Checks;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--rpc-fixture") return await RpcFixture.RunAsync();
        if (args is ["--cli-fixture", var mode, var marker]) return await CliFixture.RunAsync(mode, marker);
        if (args.Length > 1 || (args.Length == 1 && args[0] is not ("--offline" or "--http-only" or "--bash-only" or "--tools-only")))
        {
            Console.Error.WriteLine("Usage: TinyAgent.Checks [--offline | --http-only | --bash-only | --tools-only]");
            return 2;
        }
        List<(string Name, Func<Task> Run)> checks =
        [
            ("OpenAI shared fixtures and replay", ProviderChecks.OpenAIReplay),
            ("Anthropic shared fixtures and replay", ProviderChecks.AnthropicReplay),
            ("Immutable provider values", ProviderChecks.ImmutableValues),
            ("SSE framing and byte limits", ProviderChecks.FramingAndBounds),
            ("Accumulated tool argument limits", ProviderChecks.AccumulatedArgumentBounds),
            ("OpenAI malformed and incomplete streams", ProviderChecks.MalformedOpenAI),
            ("Anthropic malformed and incomplete streams", ProviderChecks.MalformedAnthropic),
            ("Terminal return without HTTP EOF", LifecycleChecks.TerminalWithoutEof),
            ("Cancellation during headers and body", LifecycleChecks.Cancellation),
            ("Deadlines during headers and body", LifecycleChecks.Deadlines),
            ("Deadline during callback delivery", LifecycleChecks.CallbackLifetime),
            ("Consumer failure releases the response", LifecycleChecks.CallbackFailure),
            ("Asynchronous cleanup before return", LifecycleChecks.CleanupBeforeReturn),
            ("Caller-owned client and provider disposal", LifecycleChecks.ClientOwnershipAndDispose),
            ("Lazy credentials and HTTP errors", LifecycleChecks.CredentialsAndHttpErrors)
        ];
        checks.AddRange(CoreChecks.Cases);
        checks.AddRange(ToolChecks.Cases);
        checks.AddRange(RpcSessionChecks.Cases);
        checks.AddRange(RpcChecks.Cases);
        checks.AddRange(HttpServerChecks.Cases);
        checks.AddRange(BashChecks.Cases);
        checks.AddRange(CliChecks.Cases);
        if (args is ["--http-only"]) checks = HttpServerChecks.Cases.ToList();
        if (args is ["--bash-only"]) checks = [.. BashChecks.Cases, .. CliChecks.Cases];
        if (args is ["--tools-only"]) checks = ToolChecks.Cases.ToList();
        if (args.Length == 0)
        {
            checks.Add(("OpenAI over real TCP", () => ProviderTcpChecks.Run(false)));
            checks.Add(("Anthropic over real TCP", () => ProviderTcpChecks.Run(true)));
            checks.Add(("HTTP Kestrel routes and disconnect over real TCP", HttpServerChecks.Tcp));
            checks.Add(("Tools run through HTTP over real TCP", () => ToolChecks.Transports(tcp: true)));
        }
        else
        {
            Console.WriteLine("Offline checks exclude the four mandatory TCP groups.");
        }
        int failed = 0;
        foreach (var (name, run) in checks)
        {
            try
            {
                await run();
                Console.WriteLine($"PASS {name}");
            }
            catch (Exception error)
            {
                failed++;
                Console.Error.WriteLine($"FAIL {name}: {error}");
            }
        }
        Console.WriteLine($"{checks.Count - failed} passed, {failed} failed.");
        return failed == 0 ? 0 : 1;
    }
}
