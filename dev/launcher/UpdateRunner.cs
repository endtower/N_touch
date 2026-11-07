using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using AbyssUpdater;

namespace AbyssLauncher;

/// <summary>Headless update mode of the same launcher executable.</summary>
public static class UpdateRunner
{
    public static async Task<int> RunAsync(string[] args)
    {
        if (args.Length == 0) return 2;
        var jobPath = Path.GetFullPath(args[0]);
        var directory = Path.GetDirectoryName(jobPath)!;
        var job = Installer.Read<Job>(jobPath);
        var recovering = args.Contains("--recover") || args.Contains("--rollback");
        var state = "installed";
        var message = "更新已安装";
        // Acquiring a lock must never enter the rollback handler of another live helper.
        FileStream installLock;
        try
        {
            var lockPath = Path.Combine(job.DataRoot, "manager", "updates", "worker.lock");
            installLock = new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException) { return 3; }
        using var heldLock = installLock;

        try
        {
            if (job.WaitPid > 0)
            {
                try
                {
                    using var parent = Process.GetProcessById(job.WaitPid);
                    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(45));
                    await parent.WaitForExitAsync(timeout.Token);
                }
                catch (ArgumentException) { /* process already exited */ }
            }
            if (recovering)
            {
                var journalPath = Path.Combine(directory, "journal.json");
                var journal = File.Exists(journalPath) ? Installer.Read<Journal>(journalPath) : null;
                if (journal?.State != "installed" || args.Contains("--rollback"))
                {
                    Installer.Restore(job, directory);
                    state = "rolled_back";
                    message = "中断的更新已恢复到安装前状态";
                }
            }
            else Installer.Apply(job, directory);
        }
        catch (Exception error)
        {
            try
            {
                Installer.Restore(job, directory);
                state = "rolled_back";
                message = "安装失败，已恢复程序文件: " + error.Message;
            }
            catch (Exception recoveryError)
            {
                Installer.WriteJson(job.Result, new { job.Id, State = "recovery_required", Message = recoveryError.Message });
                return 1;
            }
        }

        // Initial local deployment uses the same file transaction after the caller has
        // stopped the installed programs. It doesn't require an older manager to expose update APIs.
        if (args.Contains("--files-only"))
        {
            Installer.WriteJson(job.Result, new { job.Id, State = state, Message = message, job.Version });
            return state == "installed" || (args.Contains("--rollback") && state == "rolled_back") ? 0 : 1;
        }

        try
        {
            var managerUri = new Uri(job.ManagerUrl);
            if (managerUri.Host != "127.0.0.1" || managerUri.Scheme != "http")
                throw new InvalidDataException("Invalid local manager address");
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(240) };
            client.DefaultRequestHeaders.Add("X-Manager-Token", File.ReadAllText(Path.Combine(job.DataRoot, "manager", "service_token.txt")).Trim());

            async Task<bool> WaitForManager()
            {
                for (var i = 0; i < 40; i++)
                {
                    try { using var response = await client.GetAsync(job.ManagerUrl + "/v1/info"); if (response.IsSuccessStatusCode) return true; }
                    catch (HttpRequestException) { }
                    await Task.Delay(500);
                }
                return false;
            }

            void StartManager()
            {
                var source = Path.Combine(job.AppRoot, "components", "manager", "source", "manager_service.py");
                var start = new ProcessStartInfo(File.Exists(source)
                    ? Path.Combine(job.AppRoot, "components", "manager", "runtime", "python.exe")
                    : Path.Combine(job.AppRoot, "manager.exe"))
                { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = job.DataRoot };
                if (File.Exists(source)) { start.ArgumentList.Add("-u"); start.ArgumentList.Add(source); }
                start.ArgumentList.Add("--port");
                start.ArgumentList.Add(managerUri.Port.ToString());
                start.Environment["ABYSS_DATA_DIR"] = job.DataRoot;
                Process.Start(start);
            }

            if (job.Component == "manager")
            {
                StartManager();
                if (!await WaitForManager())
                {
                    Installer.Restore(job, directory);
                    state = "rolled_back";
                    message = "新管理服务未启动，已恢复上一版程序";
                    StartManager();
                    if (!await WaitForManager()) throw new IOException("管理服务恢复后仍未启动，请手动重新打开启动器");
                }
            }
            Installer.WriteJson(job.Result, new { job.Id, State = state, Message = message, job.Version });
            // finish 会解除管理服务的安装锁；先释放文件事务锁，避免下一次确认安装抢在本进程退出前启动。
            heldLock.Dispose();
            using var finish = await client.PostAsJsonAsync(job.ManagerUrl + "/v1/updates/finish", new { id = job.Id });
            finish.EnsureSuccessStatusCode();
            using var completed = JsonDocument.Parse(await finish.Content.ReadAsStringAsync());
            if (completed.RootElement.TryGetProperty("state", out var finalState)) state = finalState.GetString() ?? state;
            if (job.Component == "launcher")
            {
                var start = new ProcessStartInfo(Path.Combine(job.AppRoot, "AbyssLauncher.exe"))
                { UseShellExecute = false, WorkingDirectory = job.DataRoot };
                start.Environment["ABYSS_DATA_DIR"] = job.DataRoot;
                Process.Start(start);
            }
            return state == "installed" ? 0 : 1;
        }
        catch (Exception error)
        {
            // Keep the active job for an explicit recovery. Program backups stay available.
            Installer.WriteJson(job.Result, new { job.Id, State = state, Message = message + "；恢复进程状态失败: " + error.Message, job.Version });
            return 1;
        }
    }
}
