; CodeBuddy2API 安装包脚本 (Inno Setup 6)
; 打包 dist\CodeBuddy2API.exe 为安装程序
; 编译: ISCC.exe setup.iss

#define MyAppName "CodeBuddy2API"
#define MyAppVersion "1.0.3"
#define MyAppPublisher "yis94744"
#define MyAppURL "https://github.com/yis94744/codebuddy2api"
#define MyAppExeName "CodeBuddy2API.exe"

; 源 exe 所在目录（PyInstaller 输出目录）
; 可用 ISCC /DSourceDir=<目录> 覆盖（例如产物在临时目录、或 dist 里的 exe 正被运行中的进程占用时）
#ifndef SourceDir
#define SourceDir "dist"
#endif

; 应用图标（取自 WorkBuddy 客户端，随包分发）
; 同时用于：安装程序自身图标、快捷方式图标、卸载项图标
#define MyAppIcon "assets\icon.ico"

[Setup]
; 安装程序自身信息
AppId={{8F3A2C7E-4D10-4B2A-9E6F-1C2B9D5E7A31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=LICENSE.txt
; 安装程序自身的图标（安装包 exe、控制面板"程序和功能"里显示的都是它）
SetupIconFile={#MyAppIcon}
OutputDir=release
OutputBaseFilename=CodeBuddy2API-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 只支持 64 位 Windows（当前 PyInstaller 产物为 64 位）
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#MyAppName}
; 卸载项图标同样用客户端图标
UninstallDisplayIcon={app}\icon.ico
; 默认安装
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
; 简体中文语言文件随仓库携带（lang\），不依赖本机 Inno Setup 是否装了非官方翻译包
Name: "chs"; MessagesFile: "lang\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 桌面图标默认勾选（图标已换成 WorkBuddy 客户端图标）；取消勾选可装成纯服务
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "开机自启动 CodeBuddy2API"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 主程序 exe（onefile，体积较大）
Source: "{#SourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 应用图标：快捷方式与卸载项都指向它，因此即使 exe 内嵌图标被改也不会影响显示
Source: "{#MyAppIcon}"; DestDir: "{app}"; DestName: "icon.ico"; Flags: ignoreversion
; 不打包 config.json：程序首次启动会在 exe 同目录自动生成默认配置，
; 随包携带会把打包机的本地配置（api_key 等）带给使用者

[Icons]
; IconFilename 指向随包安装的 icon.ico —— 桌面图标即 WorkBuddy 客户端图标
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"; IconFilename: "{app}\icon.ico"

[Run]
; 安装完成后可选立即启动
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Registry]
; 开机自启（默认不勾选）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CodeBuddy2API"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[UninstallDelete]
; 卸载时保留用户生成的 config.json 与账号数据（auth 目录在外），不删除以免丢失登录态
Type: files; Name: "{app}\server.log"
