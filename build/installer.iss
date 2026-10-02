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
AppVersion=0.1.34
AppPublisher={#AppPublisher}
; 安装包/卸载程序图标
SetupIconFile=..\app\assets\app.ico
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
; 独立图标文件:快捷方式显式引用,不依赖 exe 内嵌图标的解析与系统缓存
Source: "..\app\assets\app.ico"; DestDir: "{app}"; Flags: ignoreversion
; 确保用户数据目录存在
[Dirs]
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\cache"; Permissions: users-modify
Name: "{app}\output"; Permissions: users-modify

[Icons]
; 桌面快捷方式(使用 {userdesktop} 而非 {commondesktop},与 PrivilegesRequired=lowest 一致,
; 避免非管理员安装时写 C:\Users\Public\Desktop 触发 0x80070005 拒绝访问)
; IconFilename 显式指向独立 ico:快捷方式图标不再受 exe 图标解析失败/系统缓存影响
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon; WorkingDir: "{app}"
; 开始菜单
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"

[Run]
; 刷新 Windows 图标缓存:旧版缓存会让快捷方式/任务管理器继续显示旧图标,
; ie4uinit -show(Win10/11)立即重建缓存,无需注销重启
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden
; 安装完成后可选启动
Filename: "{app}\{#ExeName}"; Description: "Launch app"; Flags: nowait postinstall shellexec

[UninstallDelete]
; 卸载时保留用户数据(历史数据库/设置/输出),只删缓存
Type: filesandordirs; Name: "{app}\cache"
