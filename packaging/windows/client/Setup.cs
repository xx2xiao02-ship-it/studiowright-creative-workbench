using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace StudioWrightClient
{
    static class Program
    {
        public static readonly string BaseDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "StudioWrightDesktop");
        public static readonly string AppDir = Path.Combine(BaseDir, "app");
        public static readonly string InstalledExe = Path.Combine(AppDir, "StudioWright.exe");
        public static readonly string NativeDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "StudioWrightClient", "native");
        static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        [STAThread]
        static void Main(string[] args)
        {
            if (args.Length == 2 && args[0] == "--self-test")
            {
                try {
                    var result = SelfTest();
                    File.WriteAllText(args[1], Json.Serialize(result), new UTF8Encoding(false));
                    Environment.Exit(result["ok"] ? 0 : 1);
                } catch { Environment.Exit(1); }
                return;
            }
            if (args.Length == 2 && args[0] == "--test-install")
            {
                try {
                    string root = Path.GetFullPath(args[1]);
                    if (!Directory.Exists(root) || Directory.EnumerateFileSystemEntries(root).Any())
                        throw new InvalidOperationException("测试目录必须存在且为空。");
                    ExtractPayload(Path.Combine(root, "app"));
                    Environment.Exit(0);
                } catch { Environment.Exit(1); }
                return;
            }
            if (args.Length == 3 && args[0] == "--test-shortcut")
            {
                try {
                    string root = Path.GetFullPath(args[1]), mode = args[2];
                    if (!Directory.Exists(root) || Directory.EnumerateFileSystemEntries(root).Any() ||
                        (mode != "on" && mode != "off" && mode != "conflict"))
                        throw new InvalidOperationException();
                    string folder = Path.Combine(root, "desktop"), app = Path.Combine(root, "app");
                    Directory.CreateDirectory(folder); Directory.CreateDirectory(app);
                    string exe = Path.Combine(app, "StudioWright.exe");
                    File.WriteAllText(exe, "isolated test target");
                    string first = Path.Combine(folder, "算力中心.lnk");
                    string second = Path.Combine(folder, "算力中心桌面版.lnk");
                    if (mode == "conflict") File.WriteAllText(first, "preserve existing file");
                    bool ready = ApplyDesktopOption(mode != "off", folder, exe);
                    if (mode == "on") ready = ready && ApplyDesktopOption(true, folder, exe) && File.Exists(first) && Directory.GetFiles(folder).Length == 1;
                    if (mode == "off") ready = ready && Directory.GetFiles(folder).Length == 0;
                    if (mode == "conflict") ready = ready && File.ReadAllText(first) == "preserve existing file" && File.Exists(second);
                    Environment.Exit(ready ? 0 : 1);
                } catch { Environment.Exit(1); }
                return;
            }
            if (args.Length == 1 && args[0] == "--install-silent")
            {
                try { Install(false); Environment.Exit(0); }
                catch { Environment.Exit(1); }
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new SetupForm());
        }

        static Dictionary<string, bool> SelfTest()
        {
            bool paths = true, readable = true, runtime = false, manager = false, extension = false, native = false, reuse = false;
            bool desktop = false, webcore = false, webforms = false, loader = false;
            bool installerIcon;
            using (var icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath))
                installerIcon = icon != null;
            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
            {
                if (stream == null) throw new InvalidOperationException();
                using (var zip = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    foreach (var entry in zip.Entries)
                    {
                        string n = entry.FullName.Replace('\\', '/');
                        if (n.StartsWith("/") || n.Split('/').Contains("..") || n.Contains(":")) paths = false;
                        if (n == "runtime/python.exe") runtime = true;
                        if (n == "manager_bridge.py") manager = true;
                        if (n == "extension/manifest.json") extension = true;
                        if (n == "native_host.py") native = true;
                        if (n == "reuse_existing.py") reuse = true;
                        if (n == "StudioWright.exe") desktop = true;
                        if (n == "Microsoft.Web.WebView2.Core.dll") webcore = true;
                        if (n == "Microsoft.Web.WebView2.WinForms.dll") webforms = true;
                        if (n == "WebView2Loader.dll") loader = true;
                        if (!String.IsNullOrEmpty(entry.Name))
                        {
                            long length = 0;
                            using (var input = entry.Open())
                            {
                                var buffer = new byte[65536];
                                int read;
                                while ((read = input.Read(buffer, 0, buffer.Length)) > 0) length += read;
                            }
                            if (length != entry.Length) readable = false;
                        }
                    }
                }
            }
            return new Dictionary<string, bool> {
                {"paths_safe", paths}, {"payload_readable", readable}, {"runtime", runtime}, {"manager", manager},
                {"extension", extension}, {"native_host", native}, {"reuse_launcher", reuse},
                {"desktop", desktop}, {"webview_core", webcore}, {"webview_forms", webforms}, {"webview_loader", loader},
                {"installer_icon", installerIcon},
                {"ok", paths && readable && runtime && manager && extension && native && reuse && desktop && webcore && webforms && loader && installerIcon}
            };
        }

        public static bool Install(bool desktopShortcut)
        {
            if (!SelfTest()["ok"]) throw new InvalidOperationException("安装包内容不完整。");
            if (Directory.Exists(AppDir) || File.Exists(AppDir))
                throw new InvalidOperationException("已存在操作端目录，未覆盖。请打开已有操作端。");
            ExtractPayload(AppDir);
            RegisterNative();
            TryShortcut();
            return ApplyDesktopOption(desktopShortcut,
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), InstalledExe);
        }

        static void ExtractPayload(string appDir)
        {
            string baseDir = Path.GetDirectoryName(appDir);
            if (Directory.Exists(appDir) || File.Exists(appDir))
                throw new InvalidOperationException("操作端目录已存在，未覆盖。");
            Directory.CreateDirectory(baseDir);
            string stage = Path.Combine(baseDir, "app-install-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(stage);
            try
            {
                using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
                using (var zip = new ZipArchive(stream, ZipArchiveMode.Read))
                {
                    foreach (var entry in zip.Entries)
                    {
                        if (String.IsNullOrEmpty(entry.Name)) continue;
                        string full = Path.GetFullPath(Path.Combine(stage, entry.FullName));
                        string prefix = Path.GetFullPath(stage) + Path.DirectorySeparatorChar;
                        if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                            throw new InvalidOperationException("安装包路径不安全。");
                        Directory.CreateDirectory(Path.GetDirectoryName(full));
                        using (var input = entry.Open())
                        using (var output = new FileStream(full, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                            input.CopyTo(output);
                    }
                }
                Directory.Move(stage, appDir);
            }
            finally
            {
                // Remove only the randomly named directory created by this invocation.
                if (Directory.Exists(stage) &&
                    Path.GetFullPath(stage).StartsWith(Path.GetFullPath(baseDir) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) &&
                    Path.GetFileName(stage).StartsWith("app-install-", StringComparison.Ordinal))
                    Directory.Delete(stage, true);
            }
        }

        static void RegisterNative()
        {
            Directory.CreateDirectory(NativeDir);
            var manifest = (Dictionary<string, object>)Json.DeserializeObject(
                File.ReadAllText(Path.Combine(AppDir, @"extension\manifest.json"), Encoding.UTF8));
            byte[] publicKey = Convert.FromBase64String((string)manifest["key"]);
            byte[] hash;
            using (var sha = SHA256.Create()) hash = sha.ComputeHash(publicKey);
            var id = new StringBuilder();
            for (int i = 0; i < 16; i++) { id.Append((char)('a' + (hash[i] >> 4))); id.Append((char)('a' + (hash[i] & 15))); }
            string cmd = Path.Combine(NativeDir, "host.cmd");
            File.WriteAllText(cmd,
                "@echo off\r\n\"%LOCALAPPDATA%\\StudioWrightDesktop\\app\\runtime\\python.exe\" -B \"%LOCALAPPDATA%\\StudioWrightDesktop\\app\\native_host.py\" %*\r\n",
                Encoding.ASCII);
            var data = new Dictionary<string, object> {
                {"name", "com.studiowright.bridge"},
                {"description", "StudioWright manual account bridge"},
                {"path", cmd}, {"type", "stdio"},
                {"allowed_origins", new[] { "chrome-extension://" + id.ToString() + "/",
                                              "chrome-extension://kbnibljeinmjmmpholicjkaiehmilbbk/" }}
            };
            string manifestPath = Path.Combine(NativeDir, "manifest.json");
            File.WriteAllText(manifestPath, Json.Serialize(data), new UTF8Encoding(false));
            using (var key = Registry.CurrentUser.CreateSubKey(@"Software\Google\Chrome\NativeMessagingHosts\com.studiowright.bridge"))
                key.SetValue("", manifestPath, RegistryValueKind.String);
        }

        static void TryShortcut()
        {
            try
            {
                string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "算力中心桌面版.lnk");
                if (File.Exists(path)) return;
                Type type = Type.GetTypeFromProgID("WScript.Shell");
                object shell = Activator.CreateInstance(type);
                object link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                Type shortcut = link.GetType();
                shortcut.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { InstalledExe });
                shortcut.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { AppDir });
                shortcut.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
            }
            catch { /* The application remains usable from its install directory. */ }
        }

        static bool ApplyDesktopOption(bool requested, string folder, string executable)
        {
            if (!requested) return true;
            try
            {
                if (!Directory.Exists(folder) || !Path.IsPathRooted(executable)) return false;
                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                object shell = Activator.CreateInstance(shellType);
                foreach (string name in new[] { "算力中心.lnk", "算力中心桌面版.lnk" })
                {
                    string path = Path.Combine(folder, name);
                    if (Directory.Exists(path)) continue;
                    if (File.Exists(path))
                    {
                        try {
                            object oldLink = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                            string oldTarget = oldLink.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, oldLink, null) as string;
                            if (!String.IsNullOrEmpty(oldTarget) && String.Equals(Path.GetFullPath(oldTarget), Path.GetFullPath(executable), StringComparison.OrdinalIgnoreCase)) return true;
                        } catch { }
                        continue;
                    }
                    string temp = Path.Combine(folder, ".studiowright-shortcut-" + Guid.NewGuid().ToString("N") + ".lnk");
                    try {
                        object link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { temp });
                        Type linkType = link.GetType();
                        linkType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { executable });
                        linkType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { Path.GetDirectoryName(executable) });
                        linkType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, link, new object[] { executable + ",0" });
                        linkType.InvokeMember("Description", BindingFlags.SetProperty, null, link, new object[] { "算力中心桌面版" });
                        linkType.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
                        try { File.Move(temp, path); }
                        catch (IOException) { if (File.Exists(path) || Directory.Exists(path)) continue; throw; }
                        return File.Exists(path);
                    } finally {
                        if (File.Exists(temp)) File.Delete(temp);
                    }
                }
                return false;
            }
            catch { return false; }
        }
    }

    sealed class SetupForm : Form
    {
        public SetupForm()
        {
            Text = "算力中心 · 操作端安装"; Width = 540; Height = 310;
            Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            StartPosition = FormStartPosition.CenterScreen;
            var info = new Label { Left = 24, Top = 20, Width = 470, Height = 115,
                Text = "安装独立桌面算力中心，自动发现并连接 Ubuntu。窗口最小化后收纳到系统托盘。\r\n\r\n安装包不包含账号、密钥、素材或对话记录，现有服务和浏览器资料不会被覆盖。" };
            var desktopShortcut = new CheckBox { Left = 24, Top = 139, Width = 280, Height = 26, Text = "在桌面创建快捷方式", Checked = true };
            var install = new Button { Left = 24, Top = 182, Width = 155, Height = 36, Text = "安装到当前用户" };
            var cancel = new Button { Left = 190, Top = 182, Width = 100, Height = 36, Text = "取消" };
            Controls.Add(info); Controls.Add(desktopShortcut); Controls.Add(install); Controls.Add(cancel);
            install.Click += delegate {
                install.Enabled = false;
                try {
                    bool shortcutReady = Program.Install(desktopShortcut.Checked);
                    string message = shortcutReady ? "桌面版已安装。接下来将打开算力中心。" :
                        "桌面版已安装，但桌面快捷方式未能创建。可以从开始菜单打开。";
                    MessageBox.Show(message, "安装完成");
                    // Program.InstalledExe is an install-time constant path, not user input.
                    Process.Start(new ProcessStartInfo(Program.InstalledExe) { UseShellExecute = true }); Close();
                } catch (Exception ex) {
                    MessageBox.Show(ex.Message, "未完成安装"); install.Enabled = true;
                }
            };
            cancel.Click += delegate { Close(); };
        }
    }

}
