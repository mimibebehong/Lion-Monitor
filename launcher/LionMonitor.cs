// Lion-Monitor 一键启动器（约 250 行，单文件，零依赖）
//
// 编译（Windows 自带编译器，C# 5 语法，不要用 $"..." / nameof / ?. 等新语法）：
//   %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe /out:LionMonitor.exe
//       /reference:System.Windows.Forms.dll /reference:System.Drawing.dll launcher\LionMonitor.cs
//
// 做的事情（方案 §3 + §12 + §16）：
//   1. 已经在跑 → 直接开浏览器（防重复启动，不占内存）
//   2. 没在跑 → 显示「正在启动…」小窗口（避免用户以为没反应而反复双击）
//   3. 后台起服务：runtime\python.exe run.py run app\main.py --server.address 0.0.0.0
//      （0.0.0.0 = 局域网同事也能访问；headless = 不自己弹浏览器；run.py 固定
//        Selector 事件循环，避免 Windows Proactor 的 accept 断链问题）
//   4. 轮询 /_stcore/health，最多 90 秒（Server 2022 冷启动慢）
//   5. 通了 → 优先用 Edge/Chrome 打开（Server 2022 默认只有 IE11，Streamlit 不支持 IE）
//   6. 超时/进程退出 → 弹窗显示 logs\lion-monitor.log 末尾 20 行 + 日志路径，并把结论写进 logs\launcher.log

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace LionMonitorLauncher
{
    internal static class Program
    {
        private const int FirstPort = 8501;
        private const int LastPort = 8510;
        internal const int StartupTimeoutMs = 90000;
        private const string HealthPath = "/_stcore/health";

        [STAThread]
        private static int Main(string[] args)
        {
            string appDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            string logDir = Path.Combine(appDir, "logs");
            string logFile = Path.Combine(logDir, "launcher.log");
            try { Directory.CreateDirectory(logDir); }
            catch (Exception) { }
            Log(logFile, "==== 启动器开始 appDir=" + appDir + " ====");

            int running = FindRunningLion();
            if (running > 0)
            {
                // 光有 /_stcore/health = 200 不够：进程活着但首页 500 时（安装时旧实例没停、
                // 程序文件被覆盖了一半），直接开浏览器只会给用户一个 Internal Server Error。
                int indexStatus = IndexStatus(running);
                if (indexStatus != 200)
                {
                    string broken = BuildBrokenMessage(appDir, running, indexStatus);
                    Log(logFile, "检测到 " + running + " 上的实例首页 HTTP " + indexStatus + "，判定为异常实例");
                    ShowError(broken);
                    return 4;
                }
                Log(logFile, "检测到 Lion-Monitor 已在运行（端口 " + running + "），直接打开浏览器");
                OpenBrowser(running, logFile);
                return 0;
            }

            int port = FindFreePort();
            if (port <= 0)
            {
                string msg = "端口 " + FirstPort + "-" + LastPort + " 全部被占用，Lion-Monitor 无法启动。\n\n"
                    + "请先在命令行执行：netstat -ano | findstr :" + FirstPort + "\n"
                    + "看清是哪个程序占用后关掉它，再双击图标。";
                Log(logFile, "失败：" + msg);
                ShowError(msg);
                return 2;
            }
            Log(logFile, "选用端口 " + port);

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            bool ok;
            bool cancelled;
            string failure;
            using (StartingForm form = new StartingForm(appDir, port, logFile))
            {
                Application.Run(form);
                ok = form.Succeeded;
                cancelled = form.Cancelled;
                failure = form.FailureDetail;
            }

            if (cancelled)
            {
                Log(logFile, "用户取消启动");
                return 0;
            }

            if (!ok)
            {
                Log(logFile, "启动失败：" + failure);
                ShowError(BuildFailureMessage(appDir, failure));
                return 3;
            }

            OpenBrowser(port, logFile);
            Log(logFile, "启动成功，端口 " + port);
            return 0;
        }

        // ------------------------------------------------------------ 端口 / 探测

        private static bool IsHealthy(int port)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(
                    "http://127.0.0.1:" + port + HealthPath);
                req.Timeout = 2000;
                req.Proxy = null;
                req.KeepAlive = false;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return resp.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>首页 HTTP 状态码；连不上返回 0。500 = 进程活着但页面打不开。</summary>
        internal static int IndexStatus(int port)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(
                    "http://127.0.0.1:" + port + "/");
                req.Timeout = 5000;
                req.Proxy = null;
                req.KeepAlive = false;
                req.AllowAutoRedirect = false;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return (int)resp.StatusCode;
                }
            }
            catch (WebException ex)
            {
                HttpWebResponse resp = ex.Response as HttpWebResponse;
                if (resp != null)
                {
                    int code = (int)resp.StatusCode;
                    resp.Close();
                    return code;
                }
                return 0;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        private static int FindRunningLion()
        {
            // Streamlit 1.66 的首页是 SPA 空壳，HTML 里没有应用标题；只能以健康检查为准。
            // 健康检查通过 = 端口上已经有一个 Streamlit 服务在跑，直接打开浏览器即可。
            for (int port = FirstPort; port <= LastPort; port++)
            {
                if (IsHealthy(port))
                {
                    return port;
                }
            }
            return 0;
        }

        private static bool PortFree(int port)
        {
            TcpListener listener = null;
            try
            {
                listener = new TcpListener(IPAddress.Any, port);
                listener.Start();
                return true;
            }
            catch (Exception)
            {
                return false;
            }
            finally
            {
                if (listener != null)
                {
                    try { listener.Stop(); }
                    catch (Exception) { }
                }
            }
        }

        private static int FindFreePort()
        {
            for (int port = FirstPort; port <= LastPort; port++)
            {
                if (PortFree(port))
                {
                    return port;
                }
            }
            return 0;
        }

        // ------------------------------------------------------------ 起服务

        internal static Process StartServer(string appDir, int port, string logFile)
        {
            string python = Path.Combine(appDir, "runtime", "python.exe");
            if (!File.Exists(python))
            {
                throw new FileNotFoundException(
                    "找不到运行时：" + python + "\n安装包可能不完整，请重新安装 Lion-Monitor。");
            }
            string mainPy = Path.Combine(appDir, "app", "main.py");
            if (!File.Exists(mainPy))
            {
                throw new FileNotFoundException(
                    "找不到程序：" + mainPy + "\n安装包可能不完整，请重新安装 Lion-Monitor。");
            }
            // run.py = 固定用 Selector 事件循环（Windows 上 Proactor 遇到 WinError 64 会
            // 关掉监听 socket，服务变成“进程在、网页打不开”，只能重启）。
            string entryPy = Path.Combine(appDir, "run.py");
            if (!File.Exists(entryPy))
            {
                throw new FileNotFoundException(
                    "找不到服务入口：" + entryPy + "\n安装包可能不完整，请重新安装 Lion-Monitor。");
            }

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = python;
            psi.Arguments = "\"" + entryPy + "\" run \"" + mainPy + "\""
                + " --server.address 0.0.0.0"
                + " --server.port " + port
                + " --server.headless true"
                + " --server.runOnSave false"
                + " --browser.gatherUsageStats false";
            psi.WorkingDirectory = appDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;

            string dataDir = Path.Combine(appDir, "data");
            try { Directory.CreateDirectory(dataDir); }
            catch (Exception) { }
            psi.EnvironmentVariables["LION_LOG_DIR"] = Path.Combine(appDir, "logs");
            psi.EnvironmentVariables["LION_DB"] = Path.Combine(dataDir, "lion.db");
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            // 服务输出统一进 lion-monitor.log（run.py 负责收集），server.log 只做兜底
            psi.EnvironmentVariables["LION_LOG_CAPTURE"] = "1";

            Process proc = Process.Start(psi);
            if (proc == null)
            {
                throw new InvalidOperationException("无法启动 python 进程。");
            }

            // 记下 PID：安装/卸载前 tools\stop_app.py 靠它精确停掉本实例
            // （不靠 PowerShell/WMI —— 现场见过 CIM 失灵、子进程无输出的机器）
            try
            {
                Directory.CreateDirectory(Path.Combine(appDir, "logs"));
                File.WriteAllText(
                    Path.Combine(appDir, "logs", "server.pid"),
                    proc.Id.ToString() + " " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                    new UTF8Encoding(false));
            }
            catch (Exception) { }

            string serverLog = Path.Combine(appDir, "logs", "server.log");
            // 每次启动重写：正常情况这个文件只有一行说明（输出都被 run.py 收进
            // lion-monitor.log 了）；只有 run.py 起不来时才会留下 Python 的原始报错。
            try
            {
                File.WriteAllText(
                    serverLog,
                    "（运行日志见 lion-monitor.log；本文件只保留服务启动前的兜底输出）"
                        + Environment.NewLine,
                    new UTF8Encoding(false));
            }
            catch (Exception) { }
            LineWriter writer = new LineWriter(serverLog);
            proc.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null) { writer.WriteLine(e.Data); }
            };
            proc.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (e.Data != null) { writer.WriteLine(e.Data); }
            };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            Log(logFile, "服务进程已起 pid=" + proc.Id + " port=" + port);
            return proc;
        }

        // ------------------------------------------------------------ 浏览器

        private static string FindBrowser()
        {
            string[,] keys = new string[,]
            {
                { @"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe", "msedge.exe" },
                { @"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "chrome.exe" }
            };
            for (int i = 0; i < keys.GetLength(0); i++)
            {
                string value = ReadAppPath(Registry.LocalMachine, keys[i, 0]);
                if (value == null) { value = ReadAppPath(Registry.CurrentUser, keys[i, 0]); }
                if (value != null && File.Exists(value)) { return value; }
            }
            string[] guesses = new string[]
            {
                @"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                @"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                @"C:\Program Files\Google\Chrome\Application\chrome.exe",
                @"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
            };
            for (int i = 0; i < guesses.Length; i++)
            {
                if (File.Exists(guesses[i])) { return guesses[i]; }
            }
            return null;
        }

        private static string ReadAppPath(RegistryKey hive, string subKey)
        {
            try
            {
                using (RegistryKey key = hive.OpenSubKey(subKey))
                {
                    if (key == null) { return null; }
                    object value = key.GetValue(null);
                    return value == null ? null : value.ToString().Trim('"');
                }
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static void OpenBrowser(int port, string logFile)
        {
            string url = "http://localhost:" + port;
            string browser = FindBrowser();
            try
            {
                if (browser != null)
                {
                    Process.Start(browser, url);
                    Log(logFile, "已用 " + Path.GetFileName(browser) + " 打开 " + url);
                }
                else
                {
                    Process.Start(url);
                    Log(logFile, "警告：没有找到 Edge/Chrome，用系统默认程序打开 " + url);
                }
            }
            catch (Exception ex)
            {
                Log(logFile, "打开浏览器失败：" + ex.Message);
                ShowError("服务已经启动，但没能自动打开浏览器。\n\n请手动打开浏览器访问：\n" + url);
            }
        }

        // ------------------------------------------------------------ 报错

        private static string BuildFailureMessage(string appDir, string detail)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("Lion-Monitor 启动失败。");
            sb.AppendLine();
            if (!string.IsNullOrEmpty(detail)) { sb.AppendLine(detail); }
            sb.AppendLine();
            sb.AppendLine("日志（logs\\lion-monitor.log 末尾 20 行）：");
            sb.AppendLine(FailureLogTail(appDir, 20));
            sb.AppendLine();
            sb.AppendLine("完整日志目录：" + Path.Combine(appDir, "logs"));
            sb.AppendLine("排障可运行 start.bat（会显示完整输出）。");
            return sb.ToString();
        }

        /// <summary>端口上有实例但首页不是 200 时的提示：告诉用户「怎么修」，而不是只报错。</summary>
        private static string BuildBrokenMessage(string appDir, int port, int indexStatus)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("Lion-Monitor 的服务在运行，但页面打不开。");
            sb.AppendLine();
            sb.AppendLine("端口 " + port + " 上的实例返回 HTTP " + indexStatus
                + (indexStatus == 500 ? "（浏览器里显示 Internal Server Error）" : "") + "。");
            sb.AppendLine();
            sb.AppendLine("常见原因：安装/升级时旧的实例没有退出，程序文件被覆盖了一半。");
            sb.AppendLine();
            sb.AppendLine("怎么修（按顺序试）：");
            sb.AppendLine("1. 关闭所有 Lion-Monitor 窗口；");
            sb.AppendLine("2. 如果装了开机自启，管理员身份运行：  sc stop LionMonitor");
            sb.AppendLine("3. 重新运行安装包（会先自动停掉旧实例，再补齐所有文件）；");
            sb.AppendLine("4. 仍然不行：开始菜单 →「Lion-Monitor 日志目录」，把日志发给技术支持。");
            sb.AppendLine();
            sb.AppendLine("服务日志（logs\\lion-monitor.log 末尾 20 行）：");
            sb.AppendLine(FailureLogTail(appDir, 20));
            sb.AppendLine();
            sb.AppendLine("日志目录：" + Path.Combine(appDir, "logs"));
            return sb.ToString();
        }

        private static string FailureLogTail(string appDir, int lines)
        {
            string main = Path.Combine(appDir, "logs", "lion-monitor.log");
            if (File.Exists(main)) { return ReadTail(main, lines); }
            // run.py 都没起来时，lion-monitor.log 还不存在，退回启动前的兜底输出
            return ReadTail(Path.Combine(appDir, "logs", "server.log"), lines);
        }

        internal static string ReadTail(string path, int lines)
        {
            try
            {
                if (!File.Exists(path)) { return "（没有 " + path + "）"; }
                string[] all = File.ReadAllLines(path);
                int start = Math.Max(0, all.Length - lines);
                StringBuilder sb = new StringBuilder();
                for (int i = start; i < all.Length; i++)
                {
                    sb.AppendLine(all[i]);
                }
                string text = sb.ToString().TrimEnd();
                return text.Length == 0 ? "（日志为空）" : text;
            }
            catch (Exception ex)
            {
                return "（日志读不出来：" + ex.Message + "）";
            }
        }

        private static void ShowError(string message)
        {
            MessageBox.Show(message, "Lion-Monitor", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        internal static void Log(string logFile, string message)
        {
            try
            {
                // 启动器日志也会一直追加，超过 1 MB 轮转一份，避免现场越攒越大
                RotateIfTooLarge(logFile);
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message
                    + Environment.NewLine;
                File.AppendAllText(logFile, line, Encoding.UTF8);
            }
            catch (Exception) { }
        }

        private static void RotateIfTooLarge(string path)
        {
            try
            {
                FileInfo info = new FileInfo(path);
                if (!info.Exists || info.Length < 1024 * 1024) { return; }
                string backup = path + ".1";
                if (File.Exists(backup)) { File.Delete(backup); }
                File.Move(path, backup);
            }
            catch (Exception) { }
        }
    }

    /// <summary>把服务输出按行追加到 logs\server.log（多线程安全）。</summary>
    internal sealed class LineWriter
    {
        private readonly string _path;
        private readonly object _gate = new object();

        public LineWriter(string path)
        {
            _path = path;
        }

        public void WriteLine(string line)
        {
            lock (_gate)
            {
                try
                {
                    File.AppendAllText(_path, line + Environment.NewLine, Encoding.UTF8);
                }
                catch (Exception) { }
            }
        }
    }

    /// <summary>「正在启动…」窗口：等服务起了自动关掉；用户点取消则杀掉服务。</summary>
    internal sealed class StartingForm : Form
    {
        private readonly string _appDir;
        private readonly int _port;
        private readonly string _logFile;
        private readonly Label _label;
        private readonly ProgressBar _bar;
        private Process _proc;
        private volatile bool _cancelled;

        public bool Succeeded { get; private set; }
        public bool Cancelled { get; private set; }
        public string FailureDetail { get; private set; }

        public StartingForm(string appDir, int port, string logFile)
        {
            _appDir = appDir;
            _port = port;
            _logFile = logFile;
            Succeeded = false;
            Cancelled = false;
            FailureDetail = "";

            Text = "Lion-Monitor";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            MaximizeBox = false;
            MinimizeBox = false;
            ClientSize = new Size(420, 130);

            _label = new Label();
            _label.Text = "正在启动 Lion-Monitor，请稍候…";
            _label.SetBounds(20, 22, 380, 40);
            Controls.Add(_label);

            _bar = new ProgressBar();
            _bar.Style = ProgressBarStyle.Marquee;
            _bar.MarqueeAnimationSpeed = 30;
            _bar.SetBounds(20, 66, 380, 18);
            Controls.Add(_bar);

            Load += OnLoad;
            FormClosing += OnClosing;
        }

        private void OnLoad(object sender, EventArgs e)
        {
            Thread worker = new Thread(Work);
            worker.IsBackground = true;
            worker.Start();
        }

        private void Work()
        {
            try
            {
                _proc = Program.StartServer(_appDir, _port, _logFile);
                DateTime deadline = DateTime.Now.AddMilliseconds(Program.StartupTimeoutMs);
                while (DateTime.Now < deadline)
                {
                    if (_cancelled) { return; }
                    if (_proc.HasExited)
                    {
                        FailureDetail = "服务进程意外退出（退出码 " + _proc.ExitCode + "）。";
                        return;
                    }
                    if (IsHealthy(_port))
                    {
                        // 健康检查只证明「服务进程活着」；首页 500 时浏览器打不开，
                        // 必须继续判首页，否则用户看到的是 Internal Server Error。
                        int indexStatus = Program.IndexStatus(_port);
                        if (indexStatus == 200)
                        {
                            Succeeded = true;
                            return;
                        }
                        FailureDetail = "服务进程已启动，但首页返回 HTTP " + indexStatus
                            + "（程序文件不完整，或安装时旧实例没停干净）。";
                        Program.Log(_logFile, "首页异常：HTTP " + indexStatus);
                        return;
                    }
                    Thread.Sleep(500);
                }
                FailureDetail = "等了 90 秒还没起来（可能被杀毒软件拦截，或磁盘太慢）。";
            }
            catch (Exception ex)
            {
                FailureDetail = ex.Message;
            }
            finally
            {
                Finish();
            }
        }

        private static bool IsHealthy(int port)
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(
                    "http://127.0.0.1:" + port + "/_stcore/health");
                req.Timeout = 2000;
                req.Proxy = null;
                req.KeepAlive = false;
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    return resp.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (Exception)
            {
                return false;
            }
        }

        private void Finish()
        {
            if (IsDisposed) { return; }
            try
            {
                BeginInvoke(new MethodInvoker(Close));
            }
            catch (Exception) { }
        }

        private void OnClosing(object sender, FormClosingEventArgs e)
        {
            if (Succeeded) { return; }
            Cancelled = true;
            _cancelled = true;
            if (_proc != null)
            {
                try
                {
                    if (!_proc.HasExited) { _proc.Kill(); }
                }
                catch (Exception) { }
            }
        }
    }
}
