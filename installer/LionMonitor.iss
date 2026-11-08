; Lion-Monitor 安装脚本（Inno Setup 6）
;
; 设计依据：docs/plans/2026-11-04-Windows发布部署方案.md
;   §2  安装目录用户可选（DisableDirPage=no 是开关）+ 六条校验
;   §3  一键启动（LionMonitor.exe）
;   §11 依赖演进（deps.lock）
;   §12 开机自启走 NSSM 服务；防火墙需 admin
;   §16 Server 2022 / Win10 / Win11 三平台
;
; 构建：dist\ 由 scripts\build_win.ps1 生成，本脚本把 dist\* 打进 {app}
; 产物：LionMonitor-Setup-{#MyAppVersion}.exe（约 330-370 MB）

#define MyAppName "Lion-Monitor"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.1"
#endif
#define MyAppPublisher "航新科技"
#define MyAppURL "http://localhost:8501"
#define MyPort "8501"
#define FirewallRule "Lion-Monitor 8501"

[Setup]
AppId={{8F3A2C41-7B6E-4D95-9E1A-5C7D2B4F6A80}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={sd}\LionMonitor
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; ★ 必须：否则 Inno 直接跳过目录页，静默装到默认路径（现场明确要能自己选目录）
DisableDirPage=no
; 每次安装都从默认目录开始，避免升级时预填旧路径、让人以为路径改不了
UsePreviousAppDir=no
; 向导页显式打开：欢迎 → 安装说明 → 选择目录 → 附加任务 → 确认 → 完成
DisableWelcomePage=no
DisableReadyPage=no
DisableFinishedPage=no
InfoBeforeFile=安装说明.txt
InfoAfterFile=安装完成.txt
; 防火墙规则 + 服务注册需要管理员；日常运行不需要
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=100
UninstallDisplayIcon={app}\LionMonitor.exe
; 安装包自身的图标（与启动器同一张图，installer\lion.ico 由 lion-icon.png 生成）
SetupIconFile=lion.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=output
OutputBaseFilename=LionMonitor-Setup-{#MyAppVersion}
; 装完不要自动重启（服务器可能不该重启）
RestartIfNeededByRun=no

[Languages]
; 中文语言文件随安装脚本一起走（Inno 6.7 默认不带 ChineseSimplified.isl）
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Messages]
; 卸载向导里明确告诉用户下一步会发生什么
ConfirmUninstall=确定要卸载 %1 吗？%n%n程序文件会被删除，但 data、logs、backup 目录默认保留；以后重装还能接着用。
UninstallStatusLabel=正在卸载 %1，请稍候（不要关闭这个窗口）…

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "autostart"; Description: "开机自动启动（注册为 Windows 服务，无需登录）"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; dist\ 由 build_win.ps1 组装：LionMonitor.exe、runtime\、app\、src\、tools\、configs\、VERSION、deps.lock
; 程序文件照常随卸载清理；演示数据单独装进 {app}\data 并标 uninsneveruninstall，
; 这样卸载后数据库和演示数据还在（文档承诺「重装后数据还在」）。
; 排除名用 demo_data 而不是 data：Inno 的 Excludes 按「任意一段路径」匹配，
; 写 data\* 会把 plotly 的 graph_objs\layout\template\data\* 也排掉，
; 结果 streamlit 装进去就 ImportError、首页 500（2026-11-06 实测）。
Source: "..\dist\*"; DestDir: "{app}"; Excludes: "demo_data\*,data\lion.db*"; Flags: recursesubdirs createallsubdirs ignoreversion
; 演示数据必须**单独一条**装进 {app}\demo_data：它被上面那条 Excludes 排掉了，
; 少了这一条 seed_demo.py 就没东西可铺，装完是空库（工作台一片空、看起来像界面不对）。
; demo_data 属于程序自带内容，随卸载删掉没关系——用户数据在 {app}\data 里另存。
Source: "..\dist\demo_data\*"; DestDir: "{app}\demo_data"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "service_install.cmd"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "service_remove.cmd"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "collect_logs.cmd"; DestDir: "{app}\installer"; Flags: ignoreversion
; 装文件之前停实例：用 dontcopy + ExtractTemporaryFile，首次安装时 {app} 里还没有它
Source: "..\tools\stop_app.py"; DestDir: "{tmp}"; Flags: dontcopy
Source: "..\start.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\stop.bat"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; 数据/日志/备份跟程序走，卸载默认保留
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\logs"; Permissions: users-modify
Name: "{app}\backup"; Permissions: users-modify

[Icons]
Name: "{group}\Lion-Monitor"; Filename: "{app}\LionMonitor.exe"
Name: "{group}\用户使用手册"; Filename: "{app}\docs\用户使用手册.pdf"
Name: "{group}\Lion-Monitor 日志目录"; Filename: "{app}\logs"
Name: "{group}\收集诊断日志（发给技术支持）"; Filename: "{app}\installer\collect_logs.cmd"; Parameters: """{app}"""
Name: "{group}\卸载 Lion-Monitor"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Lion-Monitor"; Filename: "{app}\LionMonitor.exe"; Tasks: desktopicon

[Run]
; 0) 演示版：demo_data\ 拷进 data\（只补缺失，升级不覆盖），再把库里的绝对路径改到安装目录。
;    用脚本「新建」数据文件而不是让 Inno 直接装进 data\，卸载器才不认识它们 → 卸载不删库。
Filename: "{app}\runtime\python.exe"; \
  Parameters: """{app}\tools\seed_demo.py"" --root ""{app}"""; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; StatusMsg: "正在准备演示数据…"

; 1) 防火墙放行 8501（局域网同事要能访问）—— 必须在自检之前，否则自检永远报「没有规则」
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow protocol=TCP localport={#MyPort} profile=private"; \
  Flags: runhidden waituntilterminated

; 2) 环境自检：失败会在安装最后弹窗列明细（不让用户装完打不开还摸不着头脑）
;    --expect-stopped：装文件前必须先停掉旧实例，端口上还有实例就判失败
Filename: "{app}\runtime\python.exe"; \
  Parameters: """{app}\tools\selfcheck.py"" --project-root ""{app}"" --port {#MyPort} --expect-stopped"; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; StatusMsg: "正在检查运行环境…"

; 3) 可选：注册开机自启服务（必须管理员；per-user 安装时自动跳过，否则 NSSM 会弹 UAC 卡住）
Filename: "{app}\installer\service_install.cmd"; \
  Parameters: """{app}"" {#MyPort}"; \
  Flags: runhidden waituntilterminated; Tasks: autostart; Check: IsAdminInstallMode

; 4) 立即启动
Filename: "{app}\LionMonitor.exe"; Description: "立即启动 Lion-Monitor"; \
  Flags: postinstall nowait skipifsilent

[UninstallRun]
; 顺序很重要：先停服务，否则 runtime\ 里的 dll 被占用删不掉
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "DelFirewallRule"
Filename: "{app}\installer\service_remove.cmd"; \
  Flags: runhidden waituntilterminated; RunOnceId: "DelService"

[UninstallDelete]
; 只删程序文件；data\ logs\ backup\ 由 [Code] 在卸载最后询问
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\configs"
Type: filesandordirs; Name: "{app}\installer"
Type: files; Name: "{app}\LionMonitor.exe"
Type: files; Name: "{app}\run.py"
Type: files; Name: "{app}\start.bat"
Type: files; Name: "{app}\stop.bat"
Type: files; Name: "{app}\VERSION"
Type: files; Name: "{app}\deps.lock"
Type: filesandordirs; Name: "{app}\docs"
Type: filesandordirs; Name: "{app}\launcher"
Type: filesandordirs; Name: "{app}\.streamlit"

[Code]
const
  MAX_PATH_LEN = 120;

procedure InitializeWizard();
begin
  // 多显示器 / 远程桌面下 poScreenCenter 可能把向导放到副屏或屏幕外，
  // 导致「浏览…」和「下一步」点不到；固定放在主屏左上角，保证完整可见。
  WizardForm.Position := poDesigned;
  WizardForm.Left := 100;
  WizardForm.Top := 100;
end;

/// 装文件之前先停掉正在运行的实例。
///
/// 为什么必须在复制文件之前：如果旧实例（尤其是开机自启服务）还在跑，
/// 安装器会覆盖它正在使用的 app/、src/、runtime/ 和 data/lion.db，
/// 结果就是「装完打开是 Internal Server Error」——2026-11-06 在同事机器上
/// 就撞到了：自检时报「8501 已有 Lion-Monitor 在运行」，说明装的时候旧实例没停。
///
/// 具体怎么停交给 tools\stop_app.py（只用标准库：netstat + tasklist + taskkill +
/// nssm/sc）。不用 PowerShell/WMI：现场见过 PowerShell 子进程毫无输出、Get-CimInstance
/// 直接失灵的环境，靠它停进程等于没停。
/// 取一行里第 Index 个（0 起）以空白分隔的字段。
function FieldAt(const Line: String; Index: Integer): String;
var
  I, Field, StartPos: Integer;
begin
  Result := '';
  Field := 0;
  StartPos := 0;
  for I := 1 to Length(Line) + 1 do
  begin
    if (I > Length(Line)) or (Line[I] = ' ') or (Line[I] = #9) then
    begin
      if StartPos > 0 then
      begin
        if Field = Index then
        begin
          Result := Copy(Line, StartPos, I - StartPos);
          Exit;
        end;
        Inc(Field);
        StartPos := 0;
      end;
    end
    else if StartPos = 0 then
      StartPos := I;
  end;
end;

/// 把一行写进安装停止日志（UTF-8，和 stop_app.py 的写法保持一致）。
procedure AppendStopLog(const LogFile, Text: String);
var
  Lines: TArrayOfString;
begin
  ForceDirectories(ExtractFileDir(LogFile));
  SetArrayLength(Lines, 1);
  Lines[0] := '[' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + '] ' + Text;
  SaveStringsToUTF8File(LogFile, Lines, True);
end;

/// 按端口找监听进程并结束它（**不需要本机已有 Python 运行时**）。
///
/// 为什么需要它：首次装到**新目录**时 `{app}\runtime\python.exe` 还不存在，
/// 跑不了 stop_app.py；而旧版本可能正从**另一个目录**占着 8501。只杀
/// LionMonitor.exe 管不到它（那只是启动器，真正监听的是 python.exe 子进程）。
/// 只结束 python / pythonw / LionMonitor 这几个映像名，别的程序占端口不动。
function StopByPort(Port: Integer; const LogFile: String): Boolean;
var
  OutFile, Line, Local, Pid, Image, Needle: String;
  Lines: TArrayOfString;
  I, P, ResultCode: Integer;
begin
  Result := False;
  OutFile := ExpandConstant('{tmp}\stop_app_netstat.txt');
  Exec(ExpandConstant('{cmd}'), '/c netstat -ano > "' + OutFile + '" 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if (not FileExists(OutFile)) or (not LoadStringsFromFile(OutFile, Lines)) then
  begin
    AppendStopLog(LogFile, '按端口查占用失败：netstat 没有输出');
    Exit;
  end;

  Needle := ':' + IntToStr(Port);
  Pid := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[I];
    if Pos('LISTENING', Uppercase(Line)) = 0 then
      Continue;
    Local := FieldAt(Line, 1);
    if Length(Local) < Length(Needle) then
      Continue;
    if Copy(Local, Length(Local) - Length(Needle) + 1, Length(Needle)) <> Needle then
      Continue;
    Pid := FieldAt(Line, 4);
    if Pid <> '' then
      Break;
  end;

  if Pid = '' then
  begin
    AppendStopLog(LogFile, '端口 ' + IntToStr(Port) + ' 上没有监听进程');
    Exit;
  end;

  // 核对映像名，避免误杀别人的程序
  OutFile := ExpandConstant('{tmp}\stop_app_tasklist.txt');
  Exec(ExpandConstant('{cmd}'),
       '/c tasklist /FI "PID eq ' + Pid + '" /FO CSV /NH > "' + OutFile + '" 2>nul',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Image := '';
  if FileExists(OutFile) and LoadStringsFromFile(OutFile, Lines) and (GetArrayLength(Lines) > 0) then
  begin
    Line := Lines[0];
    P := Pos('","', Line);
    if P > 2 then
      Image := Lowercase(Copy(Line, 2, P - 2));
  end;

  if (Image = 'python.exe') or (Image = 'pythonw.exe') or (Image = 'lionmonitor.exe') then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/PID ' + Pid + ' /T /F',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    AppendStopLog(LogFile, '结束占用端口 ' + IntToStr(Port) + ' 的进程 pid=' + Pid +
      '（' + Image + '）→ exit ' + IntToStr(ResultCode));
    Sleep(800);
    Result := True;
  end
  else
    AppendStopLog(LogFile, '端口 ' + IntToStr(Port) + ' 被 ' + Image +
      ' 占用，不是本程序，不处理（请手动关闭后重装）');
end;

procedure StopRunningInstance();
var
  ResultCode: Integer;
  Py, Script, Sc, TaskKill, LogFile: String;
  Stopped: Boolean;
begin
  Stopped := False;
  Py := ExpandConstant('{app}\runtime\python.exe');
  Script := ExpandConstant('{tmp}\stop_app.py');

  // 升级安装：用**旧**运行时跑 stop_app.py（这时新文件还没拷进去）
  if FileExists(Py) then
  begin
    if not FileExists(Script) then
      ExtractTemporaryFile('stop_app.py');
    if FileExists(Script) then
    begin
      Exec(Py, '"' + Script + '" --root "' + ExpandConstant('{app}') + '" --port {#MyPort}',
           '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      Stopped := True;
    end;
  end;

  if not Stopped then
  begin
    // 首次安装（本目录还没有运行时）：停服务、停启动器，
    // 再按端口兜底——旧版本可能正从**别的目录**占着 {#MyPort}。
    Sc := ExpandConstant('{sys}\sc.exe');
    Exec(Sc, 'stop LionMonitor', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    TaskKill := ExpandConstant('{sys}\taskkill.exe');
    Exec(TaskKill, '/F /IM LionMonitor.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    LogFile := ExpandConstant('{app}\logs\install_stop.log');
    AppendStopLog(LogFile, '首次安装：已停止服务与启动器进程');
    if StopByPort({#MyPort}, LogFile) then
      AppendStopLog(LogFile, '端口 {#MyPort} 已腾出');
  end;

  // 给进程/服务一点退出时间，随后自检会确认端口真的释放了
  Sleep(1500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // 复制任何文件之前调用；返回空串表示继续安装
  Result := '';
  StopRunningInstance();
end;

/// 卸载前也要停实例：程序文件被占用时，Inno 会跳过删除并留下半个目录。
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  Py, Script, LogFile: String;
begin
  Result := True;
  Py := ExpandConstant('{app}\runtime\python.exe');
  Script := ExpandConstant('{app}\tools\stop_app.py');
  if FileExists(Py) and FileExists(Script) then
  begin
    Exec(Py, '"' + Script + '" --root "' + ExpandConstant('{app}') + '" --port {#MyPort}',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end
  else
  begin
    // 运行时已经损坏/被删：退回纯 Inno 的按端口收尾，否则文件被占用删不掉
    LogFile := ExpandConstant('{app}\logs\install_stop.log');
    StopByPort({#MyPort}, LogFile);
  end;
  Sleep(1200);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // 有的 Windows 版本会在首次显示时重置窗口位置，进欢迎页后再钉一次
  if CurPageID = wpWelcome then
  begin
    WizardForm.Left := 100;
    WizardForm.Top := 100;
  end;
end;

function ValidateInstallDir(Dir: String): Boolean;
var
  Probe: String;
  Saved: Boolean;
begin
  Result := False;
  Dir := RemoveBackslashUnlessRoot(Dir);

  if Length(Dir) < 4 then
  begin
    SuppressibleMsgBox('请选一个具体的文件夹，不要装在盘根（例如 D:\LionMonitor）。', mbError, MB_OK, IDOK);
    Exit;
  end;

  if Length(Dir) > MAX_PATH_LEN then
  begin
    SuppressibleMsgBox('安装路径太长（' + IntToStr(Length(Dir)) + ' 个字符，上限 ' + IntToStr(MAX_PATH_LEN) + '）。' + #13#10 +
           '请换一个短一点的目录，例如 D:\LionMonitor。' + #13#10 + #13#10 +
           '路径过长会导致数据库和日志写入失败。', mbError, MB_OK, IDOK);
    Exit;
  end;

  if (Pos('\WINDOWS\', Uppercase(Dir)) > 0) or (Pos('\SYSTEM32', Uppercase(Dir)) > 0) then
  begin
    SuppressibleMsgBox('不能装在 Windows 系统目录里，请换一个普通目录。', mbError, MB_OK, IDOK);
    Exit;
  end;

  if not DirExists(Dir) then
  begin
    if not ForceDirectories(Dir) then
    begin
      SuppressibleMsgBox('这个目录建不出来：' + Dir + #13#10 +
             '可能没有写权限（例如装在 C:\Program Files 下）。', mbError, MB_OK, IDOK);
      Exit;
    end;
  end;

  // 真实写一个文件再删 —— 只读属性、域策略都会骗过「看起来能写」
  Probe := AddBackslash(Dir) + '.lion_write_probe';
  Saved := SaveStringToFile(Probe, 'probe', False);
  if Saved then
  begin
    DeleteFile(Probe);
  end;
  if not Saved then
  begin
    SuppressibleMsgBox('这个目录写不进文件，数据库和日志会失败：' + Dir + #13#10 + #13#10 +
           '请换一个用户有权限的目录（不要用 C:\Program Files 或盘根），' + #13#10 +
           '或以管理员身份把该目录授权给当前用户。', mbError, MB_OK, IDOK);
    Exit;
  end;

  // 中文/空格路径能用（流式界面走 HTTP），只提醒不阻止
  if Dir <> ExpandConstant('{sd}\LionMonitor') then
  begin
    if SuppressibleMsgBox('确认装到这个目录？' + #13#10 + #13#10 + Dir, mbConfirmation, MB_YESNO, IDYES) = IDNO then
      Exit;
  end;

  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
    Result := ValidateInstallDir(WizardDirValue);
end;

function GetSelfcheckTail(LogFile: String): String;
var
  Lines: TArrayOfString;
  I, Start: Integer;
  Text: String;
begin
  Result := '';
  if not LoadStringsFromFile(LogFile, Lines) then
  begin
    Result := '（没有自检记录）';
    Exit;
  end;
  Start := GetArrayLength(Lines) - 20;
  if Start < 0 then
    Start := 0;
  Text := '';
  for I := Start to GetArrayLength(Lines) - 1 do
    Text := Text + Lines[I] + #13#10;
  Result := Text;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  LogFile: String;
  StatusFile: String;
  StatusText: AnsiString;
  ExecCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    LogFile := ExpandConstant('{app}\logs\selfcheck.log');
    StatusFile := ExpandConstant('{app}\logs\selfcheck.status');
    // 只有自检真的失败（exit code 1）才弹窗；否则每次装完都误报「有问题」
    if FileExists(StatusFile) then
    begin
      if LoadStringFromFile(StatusFile, StatusText) then
      begin
        if Trim(StatusText) = '1' then
        begin
          if SuppressibleMsgBox('环境自检发现失败项（见下）。' + #13#10 + #13#10 +
                    GetSelfcheckTail(LogFile) + #13#10 +
                    '完整记录：' + LogFile + #13#10 + #13#10 +
                    '现在打开日志目录吗？', mbError, MB_YESNO, IDYES) = IDYES then
            ShellExec('open', ExpandConstant('{app}\logs'), '', '', SW_SHOWNORMAL, ewNoWait, ExecCode);
        end;
      end;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if SuppressibleMsgBox('是否一并删除数据库与备份？' + #13#10 + #13#10 +
              '选「否」保留 data\、logs\、backup\，重装后可接着用（推荐）。' + #13#10 +
              '选「是」彻底清空，数据不可恢复。', mbConfirmation,
              MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    begin
      DelTree(ExpandConstant('{app}\data'), True, True, True);
      DelTree(ExpandConstant('{app}\backup'), True, True, True);
    end;
    DelTree(ExpandConstant('{app}\logs'), True, True, True);
  end;
end;
