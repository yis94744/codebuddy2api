; CodeBuddy2API 安装包脚本 (Inno Setup 6)
; 打包 dist\CodeBuddy2API.exe 为安装程序
; 编译: ISCC.exe setup.iss

#define MyAppName "CodeBuddy2API"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "yis94744"
#define MyAppURL "https://github.com/yis94744/codebuddy2api"
#define MyAppExeName "CodeBuddy2API.exe"

; 源 exe 所在目录（PyInstaller 输出目录）
#define SourceDir "dist"

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
OutputDir=release
OutputBaseFilename=CodeBuddy2API-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 只支持 64 位 Windows（当前 PyInstaller 产物为 64 位）
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; 默认安装
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "开机自启动 CodeBuddy2API"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 主程序 exe（onefile，体积较大）
Source: "{#SourceDir}\CodeBuddy2API.exe"; DestDir: "{app}"; Flags: ignoreversion
; 依赖的配置文件随包复制一份默认值（用户安装后首启动会自动生成/合并）
Source: "{#SourceDir}\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; 安装完成后可选立即启动
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Registry]
; 开机自启（默认不勾选）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CodeBuddy2API"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[UninstallDelete]
; 卸载时保留用户生成的 config.json 与账号数据（auth 目录在外），不删除以免丢失登录态
Type: files; Name: "{app}\server.log"
