using AbyssUpdater;

var root = Path.Combine(Path.GetFullPath(args.Length > 0 ? args[0] : "tmp"), "updater-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
var passed = 0;
void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
Job MakeJob(string name)
{
    var folder = Path.Combine(root, name);
    var target = Path.Combine(folder, "program");
    var payload = Path.Combine(folder, "payload");
    Directory.CreateDirectory(target); Directory.CreateDirectory(payload);
    File.WriteAllText(Path.Combine(target, "core.exe"), "old-exe");
    Directory.CreateDirectory(Path.Combine(target, "core_internal"));
    File.WriteAllText(Path.Combine(target, "core_internal", "obsolete.dll"), "old-dependency");
    File.WriteAllText(Path.Combine(target, "manager.exe"), "unrelated-manager");
    Directory.CreateDirectory(Path.Combine(target, "memory"));
    File.WriteAllText(Path.Combine(target, "memory", "chat.db"), "user-data");
    File.WriteAllText(Path.Combine(payload, "core.exe"), "new-exe");
    Directory.CreateDirectory(Path.Combine(payload, "core_internal"));
    File.WriteAllText(Path.Combine(payload, "core_internal", "new.dll"), "new-dependency");
    return new Job
    {
        Id = name, Component = "core", Version = "0.2.0", TargetRoot = target, AppRoot = target,
        Payload = payload, Receipt = Path.Combine(target, ".versions", "core.json"),
        PreviousFiles = new[] { "core.exe", "core_internal/obsolete.dll" },
        Files = new() { ["core.exe"] = Installer.Hash(Path.Combine(payload, "core.exe")),
                        ["core_internal/new.dll"] = Installer.Hash(Path.Combine(payload, "core_internal", "new.dll")) },
    };
}
try
{
    var normal = MakeJob("normal");
    Installer.Apply(normal, Path.Combine(root, "normal", "transaction"));
    Assert(File.ReadAllText(Path.Combine(normal.TargetRoot, "core.exe")) == "new-exe", "EXE not replaced");
    Assert(!File.Exists(Path.Combine(normal.TargetRoot, "core_internal", "obsolete.dll")), "Stale dependency not removed");
    Assert(File.ReadAllText(Path.Combine(normal.TargetRoot, "manager.exe")) == "unrelated-manager", "Other component changed");
    Assert(File.ReadAllText(Path.Combine(normal.TargetRoot, "memory", "chat.db")) == "user-data", "User data changed");
    passed++;

    var interrupted = MakeJob("interrupted");
    var transaction = Path.Combine(root, "interrupted", "transaction");
    try { Installer.Apply(interrupted, transaction, _ => throw new IOException("Simulated power loss after copying EXE")); }
    catch (IOException) { }
    Installer.Restore(interrupted, transaction);
    Assert(File.ReadAllText(Path.Combine(interrupted.TargetRoot, "core.exe")) == "old-exe", "Rollback failed");
    Assert(File.ReadAllText(Path.Combine(interrupted.TargetRoot, "core_internal", "obsolete.dll")) == "old-dependency", "Old runtime missing");
    Assert(!File.Exists(Path.Combine(interrupted.TargetRoot, "core_internal", "new.dll")), "New dependency survived rollback");
    Assert(!File.Exists(interrupted.Receipt), "Rolled-back receipt survives");
    passed++;

    var corrupt = MakeJob("corrupt");
    File.WriteAllText(Path.Combine(corrupt.Payload, "core.exe"), "tampered");
    var rejected = false;
    try { Installer.Apply(corrupt, Path.Combine(root, "corrupt", "transaction")); }
    catch (InvalidDataException) { rejected = true; }
    Assert(rejected && File.ReadAllText(Path.Combine(corrupt.TargetRoot, "core.exe")) == "old-exe", "Tampered package installed");
    passed++;

    var locked = MakeJob("locked");
    transaction = Path.Combine(root, "locked", "transaction");
    using (var held = new FileStream(Path.Combine(locked.TargetRoot, "core_internal", "obsolete.dll"), FileMode.Open, FileAccess.Read, FileShare.Read))
    {
        rejected = false;
        try { Installer.Apply(locked, transaction); } catch (IOException) { rejected = true; }
        Assert(rejected, "Locked DLL did not block replacement");
        Installer.Restore(locked, transaction);
        Assert(File.ReadAllText(Path.Combine(locked.TargetRoot, "core.exe")) == "old-exe", "Loaded DLL prevented rollback");
    }
    passed++;

    foreach (var name in new[] { "../escape", "core_internal/../escape", "core_internal/NUL.dll", "core_internal/file:stream", "core_internal/file." })
    {
        rejected = false;
        try { Installer.SafePath(root, name); } catch (InvalidDataException) { rejected = true; }
        Assert(rejected, "Unsafe path accepted: " + name);
    }
    Assert(!Installer.Allowed("snowluma", "config/runtime.json"), "Snow config can be overwritten");
    Assert(Installer.Allowed("snowluma", "check-node-version.cjs"), "Official Snow runtime check is rejected");
    Assert(Installer.Allowed("snowluma", "release.json"), "Official Snow release manifest is rejected");
    Assert(!Installer.Allowed("launcher", "manager.exe"), "Launcher can replace manager");
    passed++;
    foreach (var mode in new[] { "changed", "added", "deleted" })
    {
        var local = MakeJob("local-" + mode);
        local.ProtectLocalChanges = true;
        local.ExpectedFiles = local.PreviousFiles.ToDictionary(name => name, name => Installer.Hash(Path.Combine(local.TargetRoot, name)));
        var edited = Path.Combine(local.TargetRoot, "core_internal", "obsolete.dll");
        if (mode == "changed") File.WriteAllText(edited, "user-edit");
        if (mode == "deleted") File.Delete(edited);
        if (mode == "added")
        {
            var source = Path.Combine(local.TargetRoot, "components/core/source/brain");
            Directory.CreateDirectory(source);
            File.WriteAllText(Path.Combine(source, "custom.py"), "# user source");
        }
        rejected = false;
        try { Installer.Apply(local, Path.Combine(root, "local-" + mode, "transaction")); }
        catch (InvalidDataException) { rejected = true; }
        Assert(rejected && File.ReadAllText(Path.Combine(local.TargetRoot, "core.exe")) == "old-exe", "Local " + mode + " was overwritten");
        passed++;
    }
    Assert(Installer.Allowed("core", "components/core/source/brain/att/proactive/proactive_core.py"), "Core source rejected");
    Assert(!Installer.Allowed("core", "components/core/source/.env.dev"), "User secrets allowed in Core payload");
    Assert(!Installer.Allowed("core", "components/core/source/memory/memory.db"), "User database allowed in Core payload");
    Console.WriteLine($"PASS: {passed} installer scenarios (isolation, recovery, integrity, path boundaries, local modifications)");
    return 0;
}
finally
{
    // This directory is generated under the explicit test workspace and checked before deletion.
    if (Path.GetFullPath(root).StartsWith(Path.GetFullPath(args.Length > 0 ? args[0] : "tmp") + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        Directory.Delete(root, true);
}
