; Inno Setup 安装脚本 — B站音频本地转写
; 需要 Inno Setup 6 (已安装在 C:\Program Files (x86)\Inno Setup 6\)
; 编译: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss

#define AppName "B站音频本地转写"
#define ExeName "bili-transcriber.exe"
#define AppPublisher "bili-transcriber"
#define AppURL ""

[Setup]
AppId={{B1A2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion=0.1.3
AppPublisher={#AppPublisher}
DefaultDirName={pf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=bili-transcriber-setup
Compression=lzma2/ultra64
SolidCompression=yes
; 不覆盖已有数据/缓存/输出目录
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
; PyInstaller --onedir 输出
Source: "..\dist\bili-transcriber\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "_internal\*"
; _internal 目录(Python运行时 + nvidia DLL)
Source: "..\dist\bili-transcriber\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; 确保用户数据目录存在
[Dirs]
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\cache"; Permissions: users-modify
Name: "{app}\output"; Permissions: users-modify

[Icons]
; 桌面快捷方式
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon; WorkingDir: "{app}"
; 开始菜单
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"

[Run]
; 安装完成后可选启动
Filename: "{app}\{#ExeName}"; Description: "Launch app"; Flags: nowait postinstall shellexec

[UninstallDelete]
; 卸载时保留用户数据(历史数据库/设置/输出),只删缓存
Type: filesandordirs; Name: "{app}\cache"
