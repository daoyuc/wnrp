; phpvm Windows 安装程序（Inno Setup 6）
;
; 编译方式（在 Windows 上，或用 CI）：
;   iscc /DMyAppVersion=1.0.0 ^
;        /DSourceDir=..\dist\phpvm-1.0.0-windows ^
;        /DOutputDir=..\dist packaging\phpvm.iss
;
; 也可直接执行：python packaging\build.py windows（自动探测 iscc 并编译）
;
; 升级要点：AppId 必须保持不变，Inno 才会把新版本识别为「升级」而不是并存安装；
; setup.exe 由 core/updater.py 以 /SILENT /NORESTART /CLOSEAPPLICATIONS 静默执行。

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\phpvm-" + MyAppVersion + "-windows"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define MyAppName "phpvm"
#define MyAppPublisher "daoyuc"
#define MyAppURL "https://github.com/daoyuc/wnrp"
#define MyAppExeName "phpvm.bat"

[Setup]
; AppId 固定：自动升级依赖它识别为同一应用的升级安装
AppId={{7C1F2B84-3A5D-4E6B-9C21-8F0D5A6E7B93}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
; 与既有使用习惯一致：默认安装到环境根下的 phpvm 目录
DefaultDirName=C:\wnrp\phpvm
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=phpvm-{#MyAppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; 静默升级时允许安装程序关闭正在运行的旧版本
CloseApplications=yes
RestartApplications=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; 程序文件全量覆盖；config.json 单独处理，绝不覆盖用户配置
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.json"
Source: "{#SourceDir}\config.json"; DestDir: "{app}"; \
    Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\phpvm"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 phpvm"; Filename: "{uninstallexe}"
Name: "{autodesktop}\phpvm"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 静默升级时由 core/updater.py 的升级脚本负责重新拉起，故此处跳过
Filename: "{app}\{#MyAppExeName}"; Description: "启动 phpvm"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 保留用户数据（config.json / 日志），仅清理缓存目录
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
function HasPython(): Boolean;
var
  I: Integer;
  Cands: array[0..6] of String;
begin
  Result := False;
  Cands[0] := 'C:\Python313\pythonw.exe';
  Cands[1] := 'C:\Python312\pythonw.exe';
  Cands[2] := 'C:\Python311\pythonw.exe';
  Cands[3] := ExpandConstant('{localappdata}\Programs\Python\Python313\pythonw.exe');
  Cands[4] := ExpandConstant('{localappdata}\Programs\Python\Python312\pythonw.exe');
  Cands[5] := ExpandConstant('{localappdata}\Programs\Python\Python311\pythonw.exe');
  Cands[6] := ExpandConstant('{pf}\Python312\pythonw.exe');
  for I := 0 to 6 do
  begin
    if FileExists(Cands[I]) then
    begin
      Result := True;
      Exit;
    end;
  end;
  if FileSearch('pythonw.exe', GetEnvironmentVariable('PATH')) <> '' then
    Result := True
  else if FileSearch('python.exe', GetEnvironmentVariable('PATH')) <> '' then
    Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not HasPython() then
      MsgBox('未检测到 Python 3。phpvm 需要 Python 3.10+（含 tkinter）才能运行，'
             + '请先安装 Python 并勾选 "tcl/tk and IDLE" 组件，'
             + '然后重新启动 phpvm。', mbInformation, MB_OK);
  end;
end;
