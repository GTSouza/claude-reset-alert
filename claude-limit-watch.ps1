<#
.SYNOPSIS
  claude-limit-watch.ps1  (versão Windows / PowerShell)

.DESCRIPTION
  Detecta quando o Claude Code (assinatura) reseta os limites de 5h e semanal,
  e também quando a cota "libera" antes do horário previsto (queda do % de uso).

  Fonte dos dados: `claude -p "/usage"`, que roda de forma não-interativa,
  tem CUSTO ZERO (não consome tokens nem cota) e retorna algo como:

    Current session: 15% used · resets Jun 9 at 11:50pm (America/Sao_Paulo)
    Current week (all models): 2% used · resets Jun 15 at 9pm (America/Sao_Paulo)
    Current week (Sonnet only): 0% used

    - "Current session"        -> janela de 5 horas
    - "Current week (all ...)" -> janela semanal

  Detecção de reset (por janela):
    1) o instante de reset avança além de RESET_TOLERANCE -> nova janela
    2) o % de uso cai além de DropThreshold          -> cota liberou antes do previsto
    Em ambos os casos: notificação no Windows (toast) + som + Telegram.

.PARAMETER Mode
  watch (padrão) | once | getid

.EXAMPLE
  .\claude-limit-watch.ps1
  .\claude-limit-watch.ps1 once
  .\claude-limit-watch.ps1 getid
  $env:INTERVAL=120; .\claude-limit-watch.ps1

.NOTES
  Configuração por variáveis de ambiente: INTERVAL, MODEL, DROP_THRESHOLD,
  RESET_TOLERANCE, SOUND_5H, SOUND_WEEK, STATE_DIR, TG_TOKEN/TG_CHAT_ID ou
  TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.

  Para sair de um `watch` em loop, use Ctrl+C.
  Se a execução de scripts estiver bloqueada, rode primeiro:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

[CmdletBinding()]
param(
  [ValidateSet('watch', 'once', 'getid')]
  [string]$Mode = 'watch'
)

$ErrorActionPreference = 'Stop'

# ---------- configuração ----------
function Env($name, $default) {
  $v = [Environment]::GetEnvironmentVariable($name)
  if ([string]::IsNullOrWhiteSpace($v)) { return $default } else { return $v }
}

$Interval      = [int](Env 'INTERVAL' '300')  # 5 minutos
$Model         = Env 'MODEL' 'haiku'
$DropThreshold = [int](Env 'DROP_THRESHOLD' '5')
# segundos: variação no horário de reset que NÃO conta como nova janela (evita falso positivo de "3pm" vs "2:59pm")
$ResetTolerance = [int](Env 'RESET_TOLERANCE' '600')
# Sons do sistema (System.Media.SystemSounds): Asterisk, Beep, Exclamation, Hand, Question
$Sound5h       = Env 'SOUND_5H' 'Asterisk'
$SoundWeek     = Env 'SOUND_WEEK' 'Exclamation'
$StateDir      = Env 'STATE_DIR' (Join-Path $env:USERPROFILE '.claude\limit-watch')
$LogFile       = Join-Path $StateDir 'watch.log'
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

# --- Telegram (opcional) ---
# Carrega credenciais de, na ordem: STATE_DIR\telegram.env e depois .\.env
# Aceita TG_TOKEN/TG_CHAT_ID ou TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.
function Import-DotEnv($path) {
  if (-not (Test-Path $path)) { return }
  foreach ($line in Get-Content $path) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
    $k, $v = $t -split '=', 2
    $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'")
    [Environment]::SetEnvironmentVariable($k, $v)
  }
}
Import-DotEnv (Join-Path $StateDir 'telegram.env')
Import-DotEnv (Join-Path $ScriptDir '.env')

$TgToken  = Env 'TG_TOKEN'   (Env 'TELEGRAM_BOT_TOKEN' '')
$TgChatId = Env 'TG_CHAT_ID' (Env 'TELEGRAM_CHAT_ID' '')

# ---------- utilidades ----------
function Write-Log($msg) {
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  Write-Host $line
  # -Encoding utf8: o watch.log tem emoji/·; sem isto o Windows PowerShell 5.1 grava em
  # UTF-16/ANSI e o ingest do token_monitor (que lê UTF-8) não casa o regex e perde as
  # leituras. O BOM que o PS 5.1 põe é tolerado no lado Python (_WATCHLOG_RE).
  Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Play-Sound($name) {
  try { [System.Media.SystemSounds]::$name.Play() } catch { }
}

function Show-Toast($title, $msg) {
  # Tenta toast nativo (Windows 10+); cai para balão da bandeja se indisponível.
  try {
    $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
      [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $texts = $template.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
    $texts.Item(1).AppendChild($template.CreateTextNode($msg))   | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Claude Code').Show($toast)
    return
  } catch { }
  try {
    Add-Type -AssemblyName System.Windows.Forms
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Information
    $ni.Visible = $true
    $ni.ShowBalloonTip(8000, $title, $msg, [System.Windows.Forms.ToolTipIcon]::Info)
    Start-Sleep -Milliseconds 200
  } catch { }
}

function Send-Telegram($text) {
  if ([string]::IsNullOrWhiteSpace($TgToken) -or [string]::IsNullOrWhiteSpace($TgChatId)) { return }
  foreach ($id in ($TgChatId -split ',')) {
    $id = $id.Trim()
    if ($id -eq '') { continue }
    try {
      Invoke-RestMethod -Method Post -TimeoutSec 10 `
        -Uri "https://api.telegram.org/bot$TgToken/sendMessage" `
        -Body @{ chat_id = $id; text = $text } | Out-Null
    } catch { Write-Log "⚠️  falha ao enviar Telegram p/ $id" }
  }
}

function Notify($title, $msg, $sound = 'Beep') {
  Write-Log "🔔 $title — $msg  (som: $sound)"
  Show-Toast $title $msg
  Play-Sound $sound
  Send-Telegram "🤖 $title`n$msg"
}

# Roda /usage uma vez e devolve o texto do campo "result".
function Get-UsageOnce {
  try {
    $out = & claude -p "/usage" --model $Model --output-format json 2>$null
    if (-not $out) { return $null }
    return ($out | ConvertFrom-Json).result
  } catch { return $null }
}

# O `/usage` em modo -p é uma resposta gerada pelo modelo: às vezes vem a tabela
# completa ("Current session: X% used · resets ..."), às vezes só uma frase
# genérica, às vezes vazio. Tentamos algumas vezes até obter uma resposta que
# contenha os percentuais; caso contrário, devolvemos $null para o chamador tratar.
$script:FetchRetries = if ($env:FETCH_RETRIES) { [int]$env:FETCH_RETRIES } else { 3 }
function Get-Usage {
  for ($attempt = 1; $attempt -le $script:FetchRetries; $attempt++) {
    $usage = Get-UsageOnce
    if ($usage -and ($usage -match 'Current session|% used')) { return $usage }
    if ($attempt -lt $script:FetchRetries) { Start-Sleep -Seconds 2 }
  }
  return $null
}

# Extrai @{ Pct; Reset } de uma linha do /usage que contenha $prefix.
function Parse-Line($usage, $prefix) {
  # -notmatch '(?i)only': ignora as linhas por-modelo "(... only)" p/ o prefixo genérico
  # "Current week" casar a linha agregada mesmo se o /usage relabelar o "(all ...)".
  $line = ($usage -split "`n") | Where-Object { $_ -match [regex]::Escape($prefix) -and $_ -notmatch '(?i)only' } | Select-Object -First 1
  $pct = $null; $reset = ''
  if ($line) {
    if ($line -match '(\d+)%\s*used') { $pct = [int]$Matches[1] }
    if ($line -match 'resets\s+(.+)$') { $reset = $Matches[1].Trim() }
  }
  return [pscustomobject]@{ Pct = $pct; Reset = $reset }
}

function Print-Status($usage) {
  $s = Parse-Line $usage 'Current session'
  $w = Parse-Line $usage 'Current week'
  Write-Log ("📊 5h: {0}% usado · reset {1}  |  semanal: {2}% usado · reset {3}" -f $s.Pct, $s.Reset, $w.Pct, $w.Reset)
}

# Converte um horário de reset ("Jun 10 at 3pm (America/Sao_Paulo)") em DateTime.
# O texto vem do modelo e arredonda os minutos a cada poll (3pm <-> 2:59pm),
# então comparamos instantes com tolerância em vez de comparar strings.
# Devolve $null se não conseguir interpretar.
function Reset-ToDateTime($reset) {
  if (-not $reset) { return $null }
  $s = ($reset -replace '\s*\(.*\)\s*$', '').Trim()   # remove " (timezone)"
  $s = $s -replace '\s+at\s+', ' '                     # "Jun 10 at 3pm" -> "Jun 10 3pm"
  # Insere ':00' só quando NÃO há minutos. A condição tem que testar a presença de
  # MINUTOS (\d:\d{2}), não de am/pm: todo horário termina em dígito+am/pm, então
  # '\d(am|pm)$' casaria sempre e o ':00' nunca seria inserido — 'Jun 10 3pm' (hora
  # cheia) jamais viraria '3:00pm', falhava o parse e disparava reset falso.
  if ($s -notmatch '(?i)\d:\d{2}(am|pm)$') { $s = $s -replace '(?i)(\d)(am|pm)$', '$1:00$2' }
  $s = "$s $((Get-Date).Year)"
  $fmts = @('MMM d h:mmtt yyyy','MMM dd h:mmtt yyyy','MMM d hh:mmtt yyyy','MMM dd hh:mmtt yyyy')
  $ci = [System.Globalization.CultureInfo]::InvariantCulture
  $dt = [datetime]::MinValue
  foreach ($f in $fmts) {
    if ([datetime]::TryParseExact($s, $f, $ci, [System.Globalization.DateTimeStyles]::None, [ref]$dt)) {
      # /usage só reporta resets futuros; reset de janeiro lido em dezembro recebe o
      # ano corrente e cai no passado — nesse caso é do ano seguinte.
      if ($dt -lt (Get-Date).AddDays(-1)) { $dt = $dt.AddYears(1) }
      return $dt
    }
  }
  return $null
}

# Avalia uma janela: detecta reset por mudança de horário OU por queda do %.
function Check-Window($label, $key, $pct, $reset, $sound) {
  $rf = Join-Path $StateDir "last_${key}_reset"
  $pf = Join-Path $StateDir "last_${key}_pct"
  $prevReset = if (Test-Path $rf) { (Get-Content $rf -Raw).Trim() } else { '' }
  $prevPct   = if (Test-Path $pf) { (Get-Content $pf -Raw).Trim() } else { '' }

  # Reset por horário: só conta como nova janela se o instante avançou ALÉM da
  # tolerância. Assim "3pm" virar "2:59pm" (mesmo reset, arredondado) é ignorado,
  # mas um salto real (~5h ou ~1 semana à frente) dispara. Se algum horário não
  # for interpretável, caímos para a comparação literal de strings.
  $resetAdvanced = $false
  if ($reset -and $prevReset -and ($reset -ne $prevReset)) {
    $curE = Reset-ToDateTime $reset
    $prevE = Reset-ToDateTime $prevReset
    if ($null -ne $curE -and $null -ne $prevE) {
      if (($curE - $prevE).TotalSeconds -gt $ResetTolerance) { $resetAdvanced = $true }
    } else {
      $resetAdvanced = $true
    }
  }

  if ($resetAdvanced) {
    Notify "Claude Code: $label resetou ✅" "Nova janela. Próximo reset: $reset" $sound
  }
  elseif ($null -ne $pct -and $prevPct -ne '' -and ([int]$pct -lt ([int]$prevPct - $DropThreshold))) {
    $r = if ($reset) { $reset } else { '?' }
    Notify "Claude Code: cota da janela $label liberou ⬇️" "Uso caiu de ${prevPct}% para ${pct}% (reset previsto: $r)" $sound
  }

  if ($reset) { Set-Content -Path $rf -Value $reset -NoNewline }
  if ($null -ne $pct) { Set-Content -Path $pf -Value "$pct" -NoNewline }
}

function Check-Once {
  $usage = Get-Usage
  if (-not $usage) {
    Write-Log "⚠️  /usage não retornou os percentuais (resposta vazia/genérica após $($script:FetchRetries) tentativas); tentando de novo no próximo ciclo."
    return $false
  }
  Print-Status $usage

  $s = Parse-Line $usage 'Current session'
  $w = Parse-Line $usage 'Current week'

  Check-Window "limite de 5h" "session" $s.Pct $s.Reset $Sound5h
  Check-Window "limite SEMANAL" "week" $w.Pct $w.Reset $SoundWeek
  return $true
}

# Ajuda a descobrir chat_id: liste mensagens recentes recebidas pelo bot.
function Show-ChatIds {
  if ([string]::IsNullOrWhiteSpace($TgToken)) {
    Write-Host "Defina TG_TOKEN/TELEGRAM_BOT_TOKEN primeiro (em .env ou $StateDir\telegram.env)."
    return
  }
  Write-Host "Buscando chats recentes do bot (mande uma msg ao bot/grupo antes)..."
  try {
    $resp = Invoke-RestMethod -TimeoutSec 10 -Uri "https://api.telegram.org/bot$TgToken/getUpdates"
    $resp.result | ForEach-Object { $_.message.chat } | Sort-Object id -Unique | ForEach-Object {
      $nome = if ($_.title) { $_.title } else { ("{0} {1}" -f $_.first_name, $_.last_name).Trim() }
      Write-Host ("chat_id={0}  tipo={1}  nome={2}" -f $_.id, $_.type, $nome)
    }
  } catch { Write-Host "Falha ao consultar getUpdates: $_" }
}

# ---------- execução ----------
switch ($Mode) {
  'once'  { if (Check-Once) { exit 0 } else { exit 1 } }
  'getid' { Show-ChatIds; exit 0 }
  'watch' {
    $tg = if ($TgToken) { ' Telegram ON.' } else { '' }
    Write-Log "👀 Vigiando limites do Claude Code via /usage (intervalo ${Interval}s).$tg Ctrl+C para parar."
    while ($true) {
      Check-Once | Out-Null
      Start-Sleep -Seconds $Interval
    }
  }
}
