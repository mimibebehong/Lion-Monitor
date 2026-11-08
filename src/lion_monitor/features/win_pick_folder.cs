// Windows 原生文件夹选择宿主：弹系统「浏览文件夹」并强制置顶，结果写入指定文件。
// 由 lion_monitor.features.win_picker 在首次使用时用 csc.exe 编译，二进制不入库。
using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class LionPickFolder
{
    private static readonly IntPtr HwndTopmost = new IntPtr(-1);
    private const uint SwpNosize = 0x0001;
    private const uint SwpNomove = 0x0002;
    private const uint SwpShowwindow = 0x0040;
    private const uint SwpNoactivate = 0x0010;
    private static volatile bool _finished;

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    private static extern IntPtr FindWindow(string className, string windowName);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    /// <summary>后台线程：等对话框出现后把它设成 TOPMOST 并抢前台。</summary>
    private static void RaiseToTop()
    {
        for (int i = 0; i < 1200 && !_finished; i++)
        {
            IntPtr handle = FindWindow(null, "浏览文件夹");
            if (handle == IntPtr.Zero)
            {
                handle = FindWindow(null, "Browse For Folder");
            }
            if (handle != IntPtr.Zero && IsWindowVisible(handle))
            {
                // 连续设几次，覆盖 Windows 在对话框初始化时重置样式的时机
                for (int round = 0; round < 6 && !_finished; round++)
                {
                    SetWindowPos(handle, HwndTopmost, 0, 0, 0, 0,
                        SwpNosize | SwpNomove | SwpShowwindow | SwpNoactivate);
                    BringWindowToTop(handle);
                    SetForegroundWindow(handle);
                    Thread.Sleep(120);
                }
                return;
            }
            Thread.Sleep(80);
        }
    }

    [STAThread]
    private static void Main(string[] args)
    {
        // 用法：LionPickFolder.exe <结果文件> [标题] [起始目录]
        string outFile = args.Length > 0 ? args[0] : "pick.txt";
        string title = args.Length > 1 ? args[1] : "选择数据目录";
        string initial = args.Length > 2 ? args[2] : "";

        string result = "CANCELLED";
        Application.EnableVisualStyles();

        var raiser = new Thread(RaiseToTop) { IsBackground = true };
        raiser.Start();

        try
        {
            using (var dialog = new FolderBrowserDialog())
            {
                dialog.Description = title;
                dialog.ShowNewFolderButton = true;
                if (!string.IsNullOrEmpty(initial) && Directory.Exists(initial))
                {
                    dialog.SelectedPath = initial;
                }
                if (dialog.ShowDialog() == DialogResult.OK && !string.IsNullOrEmpty(dialog.SelectedPath))
                {
                    result = dialog.SelectedPath;
                }
            }
        }
        catch (Exception)
        {
            // 任何异常都按「取消」返回，页面侧不会因此报错
        }
        finally
        {
            _finished = true;
        }

        try
        {
            File.WriteAllText(outFile, result, new UnicodeEncoding(false, true));
        }
        catch (Exception)
        {
            // 写不了结果就交给页面侧的超时/取消逻辑处理
        }
    }
}
