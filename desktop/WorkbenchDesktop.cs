using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AudiobookWorkbench.Desktop
{
    internal sealed class SessionConfig
    {
        public int Port;
        public string Token;
        public string Origin { get { return "http://127.0.0.1:" + Port.ToString(); } }
        public string StartUrl { get { return Origin + "/#token=" + Uri.EscapeDataString(Token); } }

        public static SessionConfig Read(string path)
        {
            string json = File.ReadAllText(Path.GetFullPath(path), Encoding.UTF8);
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            Dictionary<string, object> data = serializer.Deserialize<Dictionary<string, object>>(json);
            if (data == null) throw new InvalidDataException("Session JSON must be an object.");
            object portValue;
            object tokenValue;
            if (!data.TryGetValue("port", out portValue) || !(portValue is int))
                throw new InvalidDataException("Session JSON must contain an integer port.");
            int port = (int)portValue;
            if (port < 1 || port > 65535) throw new InvalidDataException("Session port must be between 1 and 65535.");
            if (!data.TryGetValue("token", out tokenValue) || !(tokenValue is string) || String.IsNullOrWhiteSpace((string)tokenValue))
                throw new InvalidDataException("Session JSON must contain a nonempty token.");
            return new SessionConfig { Port = port, Token = (string)tokenValue };
        }
    }

    internal static class NativeMethods
    {
        internal delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);
        [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern uint RegisterWindowMessage(string message);
        [DllImport("user32.dll")] internal static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr hwnd, int command);
        [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr hwnd);
        [DllImport("user32.dll")] internal static extern bool SetProcessDPIAware();
        [DllImport("user32.dll", EntryPoint = "SetProcessDpiAwarenessContext")] internal static extern bool SetProcessDpiAwarenessContext(IntPtr value);
        [DllImport("shcore.dll", EntryPoint = "SetProcessDpiAwareness")] internal static extern int SetProcessDpiAwareness(int value);
        internal const int SW_RESTORE = 9;
    }

    internal sealed class WorkbenchForm : Form
    {
        private readonly SessionConfig session;
        private readonly string profileDirectory;
        private readonly string diagnosticsPath;
        private readonly uint activationMessage;
        private WebView2 browser;
        private bool ready;

        public WorkbenchForm(SessionConfig session, string profileDirectory, string iconPath, string diagnosticsPath)
        {
            this.session = session;
            this.profileDirectory = Path.GetFullPath(profileDirectory);
            this.diagnosticsPath = diagnosticsPath;
            Text = "有声书工作台";
            Width = 1280;
            Height = 880;
            MinimumSize = new System.Drawing.Size(900, 640);
            StartPosition = FormStartPosition.CenterScreen;
            if (!String.IsNullOrWhiteSpace(iconPath)) Icon = new System.Drawing.Icon(Path.GetFullPath(iconPath));
            browser = new WebView2 { Dock = DockStyle.Fill };
            Controls.Add(browser);
            activationMessage = NativeMethods.RegisterWindowMessage("AudiobookWorkbench.Activate." + WindowsIdentity.GetCurrent().User.Value);
            FormClosed += OnFormClosed;
            Shown += OnShown;
            WriteDiagnostics("created", null);
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            TrySetAppUserModelId("LocalAudiobookWorkbench.Desktop");
            WriteDiagnostics("window-created", null);
        }

        protected override void WndProc(ref Message m)
        {
            if (activationMessage != 0 && m.Msg == activationMessage)
            {
                if (WindowState == FormWindowState.Minimized) NativeMethods.ShowWindow(Handle, NativeMethods.SW_RESTORE);
                Activate();
                NativeMethods.SetForegroundWindow(Handle);
                return;
            }
            base.WndProc(ref m);
        }

        private async void OnShown(object sender, EventArgs e)
        {
            try
            {
                Directory.CreateDirectory(profileDirectory);
                CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(null, profileDirectory, null);
                await browser.EnsureCoreWebView2Async(environment);
                CoreWebView2 core = browser.CoreWebView2;
                core.Settings.AreDevToolsEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = true;
                core.Settings.IsStatusBarEnabled = false;
                core.NavigationStarting += OnNavigationStarting;
                core.NewWindowRequested += OnNewWindowRequested;
                core.DownloadStarting += OnDownloadStarting;
                core.WebMessageReceived += OnWebMessageReceived;
                core.NavigationCompleted += OnNavigationCompleted;
                core.Navigate(session.StartUrl);
            }
            catch (Exception ex)
            {
                string safeError = ex.GetType().Name + ": WebView2 initialization failed.";
                WriteDiagnostics("initialization-error", safeError);
                string message = "有声书工作台窗口初始化失败。请确认 WebView2 Runtime 已安装，然后重新打开。\r\n错误阶段：" + ex.GetType().Name;
                MessageBox.Show(this, message, "无法打开有声书工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        }

        private void OnNavigationCompleted(object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.IsSuccess)
            {
                ready = true;
                WriteDiagnostics("ready", null);
            }
            else
            {
                ready = false;
                WriteDiagnostics("navigation-failed", e.WebErrorStatus.ToString());
            }
        }

        private void OnNavigationStarting(object sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (!IsAllowedOrigin(e.Uri)) e.Cancel = true;
        }

        private void OnNewWindowRequested(object sender, CoreWebView2NewWindowRequestedEventArgs e)
        {
            e.Handled = true;
        }

        private void OnDownloadStarting(object sender, CoreWebView2DownloadStartingEventArgs e)
        {
            e.Cancel = true;
        }

        private void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            try
            {
                if (IsAllowedOrigin(e.Source) && e.TryGetWebMessageAsString() == "audiobook-close-window") Close();
            }
            catch { }
        }

        private bool IsAllowedOrigin(string value)
        {
            Uri uri;
            return Uri.TryCreate(value, UriKind.Absolute, out uri) &&
                String.Equals(uri.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase) &&
                String.Equals(uri.Host, "127.0.0.1", StringComparison.OrdinalIgnoreCase) &&
                uri.Port == session.Port && String.IsNullOrEmpty(uri.UserInfo);
        }

        private void OnFormClosed(object sender, FormClosedEventArgs e)
        {
            WriteDiagnostics("closed", null);
            if (browser != null) { browser.Dispose(); browser = null; }
        }

        private void WriteDiagnostics(string state, string error)
        {
            if (String.IsNullOrWhiteSpace(diagnosticsPath)) return;
            try
            {
                Dictionary<string, object> info = new Dictionary<string, object>();
                info["pid"] = Process.GetCurrentProcess().Id;
                info["hwnd"] = IsHandleCreated ? Handle.ToInt64().ToString("X") : null;
                info["state"] = state;
                info["ready"] = ready;
                info["error"] = error;
                info["timestampUtc"] = DateTime.UtcNow.ToString("o");
                string path = Path.GetFullPath(diagnosticsPath);
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllText(path, new JavaScriptSerializer().Serialize(info), new UTF8Encoding(false));
            }
            catch { }
        }

        private static void TrySetAppUserModelId(string appId)
        {
            try { SetCurrentProcessExplicitAppUserModelID(appId); } catch { }
        }
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
    }

    internal static class Program
    {
        private static string Value(string[] args, string name)
        {
            for (int i = 0; i < args.Length; i++) if (String.Equals(args[i], name, StringComparison.OrdinalIgnoreCase))
            {
                if (i + 1 >= args.Length || args[i + 1].StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException("Missing value for " + name + ".");
                return args[i + 1];
            }
            return null;
        }

        private static bool Has(string[] args, string name)
        {
            foreach (string arg in args) if (String.Equals(arg, name, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private static void SetDpiAwareness()
        {
            try { if (NativeMethods.SetProcessDpiAwarenessContext(new IntPtr(-4))) return; }
            catch { }
            try { if (NativeMethods.SetProcessDpiAwareness(2) == 0) return; }
            catch { }
            try { NativeMethods.SetProcessDPIAware(); }
            catch { }
        }

        [STAThread]
        private static int Main(string[] args)
        {
            string diagnostics = null;
            bool selfTest = Has(args, "--self-test");
            try
            {
                diagnostics = Value(args, "--diagnostics-file");
                string sessionPath = Value(args, "--session");
                string profilePath = Value(args, "--profile");
                string iconPath = Value(args, "--icon");
                if (String.IsNullOrWhiteSpace(sessionPath) || String.IsNullOrWhiteSpace(profilePath) || String.IsNullOrWhiteSpace(iconPath))
                    throw new ArgumentException("Required: --session <path> --profile <path> --icon <path>.");
                SessionConfig session = SessionConfig.Read(sessionPath);
                if (selfTest)
                {
                    WriteSelfTest(diagnostics, "validated", null);
                    return 0;
                }
                SetDpiAwareness();
                string sid = WindowsIdentity.GetCurrent().User.Value;
                string mutexName = "Local\\AudiobookWorkbench.Desktop." + sid.Replace('-', '_');
                bool created;
                using (Mutex mutex = new Mutex(true, mutexName, out created))
                {
                    if (!created)
                    {
                        ActivateExistingHost();
                        WriteSelfTest(diagnostics, "activated-existing", null);
                        return 0;
                    }
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    WorkbenchForm form = new WorkbenchForm(session, profilePath, iconPath, diagnostics);
                    WriteSelfTest(diagnostics, "starting", null);
                    Application.Run(form);
                    return 0;
                }
            }
            catch (Exception ex)
            {
                WriteSelfTest(diagnostics, "error", ex.GetType().Name + ": Invalid startup arguments or session file.");
                if (selfTest) return 2;
                MessageBox.Show("有声书工作台启动参数或会话文件无效。\r\n\r\n" + ex.Message, "无法打开有声书工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 2;
            }
        }

        private static void ActivateExistingHost()
        {
            string executable = Path.GetFullPath(Application.ExecutablePath);
            string title = "有声书工作台";
            uint message = NativeMethods.RegisterWindowMessage("AudiobookWorkbench.Activate." + WindowsIdentity.GetCurrent().User.Value);
            NativeMethods.EnumWindows(delegate(IntPtr hwnd, IntPtr ignored)
            {
                uint pid;
                NativeMethods.GetWindowThreadProcessId(hwnd, out pid);
                if (pid == 0) return true;
                try
                {
                    using (Process process = Process.GetProcessById((int)pid))
                    {
                        if (!String.Equals(Path.GetFullPath(process.MainModule.FileName), executable, StringComparison.OrdinalIgnoreCase)) return true;
                    }
                    StringBuilder text = new StringBuilder(256);
                    NativeMethods.GetWindowText(hwnd, text, text.Capacity);
                    if (String.Equals(text.ToString(), title, StringComparison.Ordinal))
                    {
                        NativeMethods.PostMessage(hwnd, message, IntPtr.Zero, IntPtr.Zero);
                        return false;
                    }
                }
                catch { }
                return true;
            }, IntPtr.Zero);
        }

        private static void WriteSelfTest(string path, string state, string error)
        {
            if (String.IsNullOrWhiteSpace(path)) return;
            try
            {
                Dictionary<string, object> info = new Dictionary<string, object>();
                info["pid"] = Process.GetCurrentProcess().Id;
                info["hwnd"] = null;
                info["state"] = state;
                info["ready"] = false;
                info["error"] = error;
                info["timestampUtc"] = DateTime.UtcNow.ToString("o");
                string full = Path.GetFullPath(path);
                Directory.CreateDirectory(Path.GetDirectoryName(full));
                File.WriteAllText(full, new JavaScriptSerializer().Serialize(info), new UTF8Encoding(false));
            }
            catch { }
        }
    }
}
