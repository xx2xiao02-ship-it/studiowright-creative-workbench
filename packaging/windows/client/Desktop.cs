using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace StudioWrightDesktop
{
    static class Program
    {
        public static readonly string AppDir = Path.GetDirectoryName(Application.ExecutablePath);
        public static readonly string DataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "StudioWrightDesktop");
        public static readonly string ProfileDir = Path.Combine(DataDir, "WebView2");
        public const int DiscoveryPort = 18925;
        public const string Workbench = "http://127.0.0.1:18920/";

        public static string VersionLabel()
        {
            var version = (AssemblyInformationalVersionAttribute)Attribute.GetCustomAttribute(
                typeof(Program).Assembly, typeof(AssemblyInformationalVersionAttribute));
            return version == null || String.IsNullOrEmpty(version.InformationalVersion)
                ? "" : " · " + version.InformationalVersion;
        }

        public static Icon BrandIcon(int size)
        {
            using (var stream = typeof(Program).Assembly.GetManifestResourceStream("StudioWright.AppIcon.ico"))
            {
                if (stream == null) throw new InvalidOperationException("算力中心图标资源缺失。");
                using (var icon = new Icon(stream, size, size)) return (Icon)icon.Clone();
            }
        }

        [STAThread]
        static void Main(string[] args)
        {
            if (args.Length == 1 && args[0] == "--self-check")
            {
                Environment.Exit(SelfCheck() ? 0 : 1); return;
            }
            if (args.Length == 1 && args[0] == "--discovery-check")
            {
                try { Environment.Exit(Discovery.Scan().Count > 0 ? 0 : 1); }
                catch { Environment.Exit(1); }
                return;
            }
            bool created;
            using (var mutex = new Mutex(true, @"Local\StudioWrightDesktop.Instance", out created))
            {
                if (!created)
                {
                    try { using (var existing = EventWaitHandle.OpenExisting(@"Local\StudioWrightDesktop.Show")) existing.Set(); }
                    catch (WaitHandleCannotBeOpenedException) { }
                    return;
                }
                using (var show = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\StudioWrightDesktop.Show"))
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    var window = new DesktopWindow();
                    var listener = new Thread(delegate() {
                        while (show.WaitOne())
                        {
                            if (window.IsDisposed) return;
                            try { window.BeginInvoke((Action)window.RestoreWindow); } catch (InvalidOperationException) { }
                        }
                    });
                    listener.IsBackground = true; listener.Start();
                    Application.Run(window);
                    show.Set();
                }
            }
        }

        static bool SelfCheck()
        {
            using (var icon = BrandIcon(16))
            {
              return icon.Width == 16 && icon.Height == 16 &&
                   File.Exists(Path.Combine(AppDir, "Microsoft.Web.WebView2.Core.dll")) &&
                   File.Exists(Path.Combine(AppDir, "Microsoft.Web.WebView2.WinForms.dll")) &&
                   File.Exists(Path.Combine(AppDir, "WebView2Loader.dll")) &&
                   Discovery.IsPrivate(IPAddress.Parse("192.168.1.5")) &&
                   !Discovery.IsPrivate(IPAddress.Parse("8.8.8.8"));
            }
        }
    }

    sealed class Offer
    {
        public string Address;
        public string Host;
        public string User;
        public int SshPort;
        public override string ToString() { return Host + "（" + Address + "）"; }
    }

    static class Discovery
    {
        static readonly byte[] Probe = Encoding.ASCII.GetBytes("STUDIOWRIGHT_DISCOVER_V1\n");

        public static bool IsPrivate(IPAddress ip)
        {
            byte[] b = ip.GetAddressBytes();
            return b.Length == 4 && (b[0] == 10 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31) ||
                (b[0] == 192 && b[1] == 168) || (b[0] == 169 && b[1] == 254));
        }

        static Offer Parse(byte[] packet, IPEndPoint peer)
        {
            if (packet.Length > 1024 || !IsPrivate(peer.Address) || peer.Port != Program.DiscoveryPort) return null;
            try
            {
                var data = new JavaScriptSerializer().DeserializeObject(Encoding.UTF8.GetString(packet)) as Dictionary<string, object>;
                if (data == null || !data.ContainsKey("product") || Convert.ToString(data["product"]) != "StudioWright" ||
                    !data.ContainsKey("protocol") || Convert.ToInt32(data["protocol"]) != 1 ||
                    !data.ContainsKey("workbench_port") || Convert.ToInt32(data["workbench_port"]) != 18920) return null;
                string host = Convert.ToString(data["host"]), user = Convert.ToString(data["user"]);
                int port = Convert.ToInt32(data["ssh_port"]);
                if (!Regex.IsMatch(host, @"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$") ||
                    !Regex.IsMatch(user, @"^[A-Za-z_][A-Za-z0-9_-]{0,63}$") || port < 1 || port > 65535) return null;
                return new Offer { Address = peer.Address.ToString(), Host = host, User = user, SshPort = port };
            }
            catch { return null; }
        }

        public static List<Offer> Scan()
        {
            var sockets = new List<UdpClient>();
            var found = new Dictionary<string, Offer>();
            try
            {
                foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
                {
                    if (nic.OperationalStatus != OperationalStatus.Up || nic.NetworkInterfaceType == NetworkInterfaceType.Loopback ||
                        nic.NetworkInterfaceType == NetworkInterfaceType.Tunnel) continue;
                    IPInterfaceProperties properties;
                    try { properties=nic.GetIPProperties(); } catch (NetworkInformationException) { continue; }
                    foreach (var address in properties.UnicastAddresses)
                    {
                        if (address.Address.AddressFamily != AddressFamily.InterNetwork || !IsPrivate(address.Address)) continue;
                        try
                        {
                            if (address.IPv4Mask==null) continue;
                            byte[] ip = address.Address.GetAddressBytes(), mask = address.IPv4Mask.GetAddressBytes(), broadcast = new byte[4];
                            if (mask[0] == 255 && mask[1] == 255 && mask[2] == 255 && mask[3] >= 254) continue;
                            for (int i = 0; i < 4; i++) broadcast[i] = (byte)(ip[i] | (byte)~mask[i]);
                            var udp = new UdpClient(new IPEndPoint(address.Address, 0));
                            udp.EnableBroadcast = true;
                            udp.Send(Probe, Probe.Length, new IPEndPoint(new IPAddress(broadcast), Program.DiscoveryPort));
                            sockets.Add(udp);
                        }
                        catch (SocketException) { }
                        catch (NotSupportedException) { }
                    }
                }
                var end = DateTime.UtcNow.AddSeconds(3);
                while (DateTime.UtcNow < end)
                {
                    bool pending = false;
                    foreach (var udp in sockets)
                    {
                        while (udp.Available > 0)
                        {
                            pending = true;
                            var peer = new IPEndPoint(IPAddress.Any, 0);
                            byte[] bytes = udp.Receive(ref peer);
                            Offer offer = Parse(bytes, peer);
                            if (offer != null) found[offer.Address + ":" + offer.SshPort] = offer;
                        }
                    }
                    if (!pending) Thread.Sleep(35);
                }
            }
            finally { foreach (var socket in sockets) socket.Close(); }
            return found.Values.OrderBy(x => x.Host).ThenBy(x => x.Address).ToList();
        }
    }

    static class Connection
    {
        public static bool PortOpen(int port)
        {
            try { using (var client = new TcpClient()) { var a = client.BeginConnect("127.0.0.1", port, null, null);
                bool ok = a.AsyncWaitHandle.WaitOne(300); if (ok) client.EndConnect(a); return ok; } }
            catch { return false; }
        }

        public static bool Health(int port, string path, string marker)
        {
            try {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + path);
                request.Proxy = null; request.Timeout = 1800;
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    var content = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                    return content != null && content.ContainsKey("application") && Convert.ToString(content["application"]) == marker;
                }
            } catch { return false; }
        }

        public static bool FlowHealth()
        {
            try {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:18888/health");
                request.Proxy = null; request.Timeout = 1800;
                request.Headers[HttpRequestHeader.Authorization] = "Bearer sw-readonly-probe";
                request.Headers["X-Studiowright-Probe"] = "1";
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    var content = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                    return content != null && content.ContainsKey("application") && Convert.ToString(content["application"]) == "studiowright-console";
                }
            }
            catch (WebException error) {
                try { using (var response = error.Response as HttpWebResponse) {
                    if (response == null || response.StatusCode != HttpStatusCode.Unauthorized) return false;
                    using (var reader = new StreamReader(response.GetResponseStream())) {
                        var content = new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                        return content != null && content.ContainsKey("error") && Convert.ToString(content["error"]) == "API Key 无效或已撤销。";
                    }
                }} catch { return false; }
            } catch { return false; }
        }

        public static bool WorkbenchReady() { return Health(18920, "/api/health", "unified-workbench"); }
        public static bool StackReady() { return WorkbenchReady() && FlowHealth() && Health(18922, "/health", "studiowright-unified-api"); }

        public static void StartManager()
        {
            if (Health(18890, "/health", "studiowright-chrome-manager") || PortOpen(18890)) return;
            string python = Path.Combine(Program.AppDir, @"runtime\pythonw.exe");
            string script = Path.Combine(Program.AppDir, "manager_bridge.py");
            if (!File.Exists(python) || !File.Exists(script)) return;
            var start = new ProcessStartInfo(python, "-B \"" + script + "\"");
            start.UseShellExecute = false; start.CreateNoWindow = true; start.WorkingDirectory = Program.AppDir;
            try { Process.Start(start); } catch { }
        }

        public static bool TryReuseConfiguredConnection()
        {
            string python=Path.Combine(Program.AppDir,@"runtime\python.exe");
            string script=Path.Combine(Program.AppDir,"reuse_existing.py");
            if (!File.Exists(python) || !File.Exists(script)) return false;
            try {
                var start=new ProcessStartInfo(python,"-B \"" + script + "\" --no-browser");
                start.UseShellExecute=false;start.CreateNoWindow=true;start.WorkingDirectory=Program.AppDir;
                using (var process=Process.Start(start)) {
                    if (!process.WaitForExit(25000)) { try { process.Kill(); } catch { } return false; }
                    return process.ExitCode==0 && WorkbenchReady();
                }
            } catch { return false; }
        }
    }

    sealed class ConnectDialog : Form
    {
        readonly ComboBox servers = new ComboBox { Left=150, Top=66, Width=275, DropDownStyle=ComboBoxStyle.DropDownList };
        readonly Button scan = new Button { Left=435, Top=65, Width=85, Height=29, Text="重新查找" };
        readonly TextBox host = new TextBox { Left=150, Top=108, Width=275 };
        readonly TextBox user = new TextBox { Left=150, Top=146, Width=155 };
        readonly TextBox port = new TextBox { Left=375, Top=146, Width=50, Text="22" };
        readonly TextBox password = new TextBox { Left=150, Top=184, Width=275, UseSystemPasswordChar=true };
        readonly Label state = new Label { Left=25, Top=309, Width=500, Height=65, Text="正在查找同一局域网内的 Ubuntu 算力中心……" };
        readonly Button connect = new Button { Left=25, Top=257, Width=142, Height=35, Text="连接算力中心" };
        readonly Button cancel = new Button { Left=180, Top=257, Width=100, Height=35, Text="取消" };
        bool connecting;
        public IConnectionTunnel NewTunnel;

        public ConnectDialog()
        {
            Text="连接 Ubuntu 算力中心"; Width=570; Height=420; StartPosition=FormStartPosition.CenterParent;
            AcceptButton=connect; CancelButton=cancel;
            Controls.Add(new Label { Left=25, Top=20, Width=500, Height=33, Text="自动发现只提供地址；首次连接需确认服务器身份。" });
            Controls.Add(new Label { Left=25, Top=69, Width=115, Text="发现的服务" });
            Controls.Add(new Label { Left=25, Top=111, Width=115, Text="Ubuntu 地址" });
            Controls.Add(new Label { Left=25, Top=149, Width=115, Text="Ubuntu 用户名" });
            Controls.Add(new Label { Left=320, Top=149, Width=50, Text="端口" });
            Controls.Add(new Label { Left=25, Top=187, Width=115, Text="Ubuntu 密码" });
            Controls.Add(new Label { Left=150, Top=217, Width=375, Text="密码仅用于本次连接；已配置 SSH 密钥可留空。" });
            Controls.Add(servers);Controls.Add(scan);Controls.Add(host);Controls.Add(user);Controls.Add(port);
            Controls.Add(password);
            Controls.Add(connect);Controls.Add(cancel);Controls.Add(state);
            servers.SelectedIndexChanged += delegate { var offer=servers.SelectedItem as Offer; if (offer!=null) {
                host.Text=offer.Address; user.Text=offer.User; port.Text=offer.SshPort.ToString(); } };
            scan.Click += delegate { Find(); }; connect.Click += delegate { Connect(); };
            cancel.Click += delegate { DialogResult=DialogResult.Cancel; Close(); };
            FormClosed += delegate { password.Clear(); if (DialogResult!=DialogResult.OK) StopTunnel(); };
            Shown += delegate { Find(); };
        }

        void StopTunnel()
        {
            if(NewTunnel==null) return;
            NewTunnel.Dispose();
            NewTunnel=null;
        }

        async void Find()
        {
            scan.Enabled=false; state.Text="正在查找局域网中的算力中心……";
            try {
                var found=await Task.Run(() => Discovery.Scan());
                if (IsDisposed || connecting) return;
                servers.Items.Clear(); foreach (var offer in found) servers.Items.Add(offer);
                if (servers.Items.Count==1) servers.SelectedIndex=0;
                state.Text=servers.Items.Count==0 ? "未发现服务器，可输入地址，或检查两台设备是否在同一局域网。" :
                    "已发现 " + servers.Items.Count + " 台服务器。请选择后连接。";
            } catch { if (!IsDisposed && !connecting) state.Text="查找未完成，可以手动填写地址。"; }
            finally { if (!IsDisposed && !connecting) scan.Enabled=true; }
        }

        bool ApproveHost(string endpoint, string fingerprint)
        {
            if (IsDisposed || !IsHandleCreated) return false;
            try {
                return (bool)Invoke((Func<bool>)delegate {
                    return !IsDisposed && MessageBox.Show(this,
                        "服务器：" + endpoint + "\n指纹：" + fingerprint +
                        "\n\n请与 Ubuntu 管理员提供的指纹核对。确认一致后才发送密码。",
                        "确认 Ubuntu 服务器身份", MessageBoxButtons.YesNo, MessageBoxIcon.Question,
                        MessageBoxDefaultButton.Button2) == DialogResult.Yes;
                });
            } catch (InvalidOperationException) { return false; }
        }

        async void Connect()
        {
            if (connecting) return;
            IPAddress address; int sshPort;
            string h=host.Text.Trim(), u=user.Text.Trim();
            if (!IPAddress.TryParse(h,out address) || !Discovery.IsPrivate(address) ||
                !Regex.IsMatch(u,@"^[A-Za-z_][A-Za-z0-9_-]{0,63}$") || !Int32.TryParse(port.Text.Trim(),out sshPort) || sshPort<1 || sshPort>65535)
            { state.Text="请填写有效的内网地址、Ubuntu 用户名和端口。"; return; }
            if (Connection.PortOpen(18920) || Connection.PortOpen(18888) || Connection.PortOpen(18922))
            { state.Text="本机连接端口正在使用，请先确认现有算力中心连接。"; return; }
            h=address.ToString();
            string secret=password.Text;
            password.Clear();
            string ssh=Path.Combine(Environment.SystemDirectory,@"OpenSSH\ssh.exe");
            if (secret.Length==0 && !File.Exists(ssh)) { state.Text="密钥连接需要 Windows OpenSSH；也可在此填写 Ubuntu 密码连接。"; return; }
            connecting=true;
            connect.Enabled=scan.Enabled=false;
            servers.Enabled=host.Enabled=user.Enabled=port.Enabled=password.Enabled=false;
            state.Text="正在连接 Ubuntu 并检查算力中心……";
            string forwards=" -L 127.0.0.1:18920:127.0.0.1:18920 -L 127.0.0.1:18888:127.0.0.1:18888 -L 127.0.0.1:18922:127.0.0.1:18922";
            try {
                var ready=await Task.Run(() => {
                    bool passwordLogin=secret.Length!=0;
                    IConnectionTunnel candidate=null;
                    try {
                        if (passwordLogin) {
                            int[] ports={18920,18888,18922};
                            candidate=PanelSsh.Open(h,sshPort,u,secret,Path.Combine(Program.DataDir,"UbuntuHostKeys"),
                                fingerprint => ApproveHost(h+":"+sshPort,fingerprint),ports,ports);
                        } else {
                            var start=new ProcessStartInfo(ssh,"-N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -p " + sshPort + forwards + " " + u + "@" + h);
                            start.UseShellExecute=false; start.CreateNoWindow=true;
                            candidate=new OpenSshTunnel(Process.Start(start));
                        }
                        for(int i=0;i<15;i++) {
                            if (IsDisposed) return null;
                            if (!candidate.IsAlive) break;
                            if (Connection.StackReady()) {
                                var result=candidate; candidate=null; return result;
                            }
                            Thread.Sleep(400);
                        }
                        throw new InvalidDataException(passwordLogin ?
                            "已尝试连接，但算力中心服务未通过检查。请核对 Ubuntu 服务状态。" :
                            "密钥连接未完成。请核对已信任的服务器和 SSH 密钥，或在面板中填写密码。");
                    } finally { secret=null; if(candidate!=null) candidate.Dispose(); }
                });
                if (IsDisposed) { if(ready!=null) ready.Dispose(); return; }
                if (ready!=null) { NewTunnel=ready; Connection.StartManager(); DialogResult=DialogResult.OK; Close(); }
            } catch (Renci.SshNet.Common.SshAuthenticationException) {
                if (!IsDisposed) state.Text="Ubuntu 拒绝了密码登录，请核对用户名、密码及服务器登录设置。";
            } catch (InvalidDataException error) {
                if (!IsDisposed) state.Text=error.Message;
            } catch {
                if (!IsDisposed) state.Text="无法建立连接，请核对 Ubuntu 地址、端口及本机网络。";
            } finally {
                connecting=false;
                if (!IsDisposed) {
                    connect.Enabled=scan.Enabled=true;
                    servers.Enabled=host.Enabled=user.Enabled=port.Enabled=password.Enabled=true;
                }
            }
        }
    }

    sealed class DesktopWindow : Form
    {
        readonly WebView2 web = new WebView2 { Dock=DockStyle.Fill };
        readonly Label message = new Label { Dock=DockStyle.Fill, TextAlign=ContentAlignment.MiddleCenter, Text="正在连接算力中心……" };
        readonly NotifyIcon tray;
        readonly ToolStripStatusLabel status = new ToolStripStatusLabel("正在连接…");
        IConnectionTunnel tunnel;
        bool exiting, connectionBusy, connectionDialogOpen, wasDisconnected, monitoring, navigationFailed;
        readonly System.Windows.Forms.Timer connectionTimer = new System.Windows.Forms.Timer { Interval=5000 };
        DateTime nextReconnect = DateTime.MinValue;
        int reconnectFailures;

        public DesktopWindow()
        {
            Text="算力中心" + Program.VersionLabel(); Width=1380; Height=920; MinimumSize=new Size(800,560); StartPosition=FormStartPosition.CenterScreen;
            Icon=Program.BrandIcon(32);
            var bar=new StatusStrip(); var reconnect=new ToolStripDropDownButton("连接");
            reconnect.DropDownItems.Add("重新连接 Ubuntu",null,delegate { OpenConnection(); });
            reconnect.DropDownItems.Add("刷新页面",null,delegate { if (web.CoreWebView2!=null) web.Reload(); });
            bar.Items.Add(reconnect);bar.Items.Add(status);
            Controls.Add(message);Controls.Add(bar);
            var menu=new ContextMenuStrip(); menu.Items.Add("打开算力中心",null,delegate { RestoreWindow(); });
            menu.Items.Add("退出",null,delegate { exiting=true; Close(); });
            tray=new NotifyIcon { Icon=Program.BrandIcon(16), Text="算力中心", Visible=true, ContextMenuStrip=menu };
            tray.DoubleClick+=delegate { RestoreWindow(); };
            Resize+=delegate { if (WindowState==FormWindowState.Minimized) Hide(); };
            FormClosing+=delegate(object sender, FormClosingEventArgs e) {
                if (!exiting && e.CloseReason==CloseReason.UserClosing) { e.Cancel=true; Hide(); }
            };
            FormClosed+=delegate { connectionTimer.Stop(); connectionTimer.Dispose(); tray.Visible=false;tray.Dispose();if(tunnel!=null) tunnel.Dispose(); };
            connectionTimer.Tick+=async delegate { await CheckConnection(); };
            Shown+=async delegate {
                connectionBusy=true;
                bool ready=await Task.Run(() => Connection.WorkbenchReady());
                if (!ready) ready=await Task.Run(() => Connection.TryReuseConfiguredConnection());
                if (IsDisposed) return;
                if (ready) { Connection.StartManager(); ShowWorkbench(); } else OpenConnection();
                connectionBusy=false;
                connectionTimer.Start();
            };
        }

        public void RestoreWindow() { if (IsDisposed) return; Show(); WindowState=FormWindowState.Normal; Activate(); }

        void OpenConnection()
        {
            if (connectionDialogOpen || monitoring) return;
            connectionDialogOpen=true;
            try {
                using(var dialog=new ConnectDialog()) {
                    if(dialog.ShowDialog(this)==DialogResult.OK) {
                        if(tunnel!=null) tunnel.Dispose();
                        tunnel=dialog.NewTunnel;
                        reconnectFailures=0; wasDisconnected=false;
                        ShowWorkbench();
                    }
                    else if (!Connection.WorkbenchReady()) { status.Text="尚未连接 · 将自动重试";message.Text="连接中断，将自动重试。也可点击左下角“连接”。"; }
                }
            } finally { connectionDialogOpen=false; }
        }

        async Task CheckConnection()
        {
            if (connectionBusy || connectionDialogOpen || monitoring || exiting || IsDisposed) return;
            monitoring=true;
            try {
                bool healthy=await Task.Run(() => Connection.StackReady());
                if (IsDisposed || exiting) return;
                if (healthy) {
                    bool recovered=wasDisconnected;
                    wasDisconnected=false; reconnectFailures=0; nextReconnect=DateTime.MinValue;
                    status.Text="已连接 Ubuntu 算力中心 · 断线后自动重连";
                    if (recovered) {
                        if (web.CoreWebView2==null || navigationFailed) ShowWorkbench();
                        else await web.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new CustomEvent('studiowright-connection',{detail:{connected:true}}));");
                    }
                    return;
                }
                wasDisconnected=true;
                status.Text="连接已断开 · 正在自动重连，页面内容已保留";
                if (web.CoreWebView2!=null)
                    await web.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new CustomEvent('studiowright-connection',{detail:{connected:false}}));");
                if (DateTime.UtcNow<nextReconnect) return;
                bool ready=await Task.Run(() => {
                    if (tunnel!=null) {
                        if (Connection.StackReady()) return true;
                        if (!tunnel.Reconnect()) return false;
                        for (int i=0;i<10;i++) {
                            if (Connection.StackReady()) return true;
                            Thread.Sleep(400);
                        }
                        return false;
                    }
                    return Connection.TryReuseConfiguredConnection();
                });
                if (IsDisposed || exiting) return;
                if (ready) {
                    wasDisconnected=false; reconnectFailures=0;
                    status.Text="连接已恢复 · 已重新读取状态";
                    if (web.CoreWebView2==null || navigationFailed) ShowWorkbench();
                    else await web.CoreWebView2.ExecuteScriptAsync("window.dispatchEvent(new CustomEvent('studiowright-connection',{detail:{connected:true}}));");
                } else {
                    reconnectFailures++;
                    int seconds=Math.Min(30,5*reconnectFailures);
                    nextReconnect=DateTime.UtcNow.AddSeconds(seconds);
                    status.Text="暂时无法连接 Ubuntu · "+seconds+" 秒后自动重试；请确认主机已开机";
                }
            } catch {
                nextReconnect=DateTime.UtcNow.AddSeconds(15);
                if (!IsDisposed) status.Text="连接中断 · 15 秒后自动重试";
            } finally { monitoring=false; }
        }

        async void ShowWorkbench()
        {
            try {
                if (web.CoreWebView2==null) {
                    var environment=await CoreWebView2Environment.CreateAsync(null,Program.ProfileDir);
                    await web.EnsureCoreWebView2Async(environment);
                    web.CoreWebView2.NavigationCompleted+=delegate(object sender, CoreWebView2NavigationCompletedEventArgs e) { navigationFailed=!e.IsSuccess; };
                    web.CoreWebView2.NewWindowRequested+=delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e) {
                        e.Handled=true;
                        Uri uri;
                        if (Uri.TryCreate(e.Uri,UriKind.Absolute,out uri) && (uri.Scheme=="http" || uri.Scheme=="https")) {
                            var child=new EmbeddedWindow(uri,environment); child.Show(this);
                        }
                    };
                }
                if (!Controls.Contains(web)) { Controls.Add(web);web.BringToFront(); }
                web.Source=new Uri(Program.Workbench); status.Text="已连接 Ubuntu 算力中心 · 最小化后收纳到系统托盘";
            } catch { status.Text="桌面窗口启动失败";message.Text="无法启动桌面显示组件，请检查 WebView2 运行环境。"; }
        }
    }

    sealed class EmbeddedWindow : Form
    {
        public EmbeddedWindow(Uri uri, CoreWebView2Environment environment)
        {
            Text="算力中心 · 附加窗口";Width=1200;Height=800;StartPosition=FormStartPosition.CenterParent;
            Icon=Program.BrandIcon(32);
            var view=new WebView2 { Dock=DockStyle.Fill };Controls.Add(view);
            Shown+=async delegate {
                try { await view.EnsureCoreWebView2Async(environment);
                    view.CoreWebView2.NewWindowRequested+=delegate(object sender,CoreWebView2NewWindowRequestedEventArgs e) {
                        e.Handled=true;Uri next;
                        if(Uri.TryCreate(e.Uri,UriKind.Absolute,out next) && (next.Scheme=="http" || next.Scheme=="https"))
                            new EmbeddedWindow(next,environment).Show(this);
                    };
                    view.Source=uri;
                } catch { Text="附加窗口无法打开"; }
            };
        }
    }
}
