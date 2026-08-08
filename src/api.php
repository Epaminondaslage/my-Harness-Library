<?php
// =============================================================================
// api.php — backend do editor do inventario Claude Code
// -----------------------------------------------------------------------------
// Roda no pool PHP-FPM dedicado "claude-inventory", como o usuario dono do
// ~/.claude — assim nenhuma permissao de arquivo precisa ser alargada para o
// www-data (que nem atravessa o home, 750).
//
// Acoes (todas POST, JSON in / JSON out):
//   read     {file}                        -> conteudo + mtime
//   save     {file, content, password, mtime}
//   create   {file, content, password}     -> cria .md novo no escopo permitido
//   history  {file}                        -> lista de revisoes (.bak)
//   revision {file, rev}                   -> conteudo de uma revisao
//   restore  {file, rev, password}         -> volta o arquivo a uma revisao
//
// Seguranca:
//   - Allowlist de raizes: skills/, agents/, commands/ dentro de ~/.claude.
//   - realpath() conferido contra a raiz permitida: barra ../ e symlink.
//   - Escrita exige senha (hash bcrypt em ~/.claude-inventory-auth.php).
//   - Frontmatter validado: skill/agent sem name+description e recusado,
//     porque o Claude Code simplesmente deixa de carregar o recurso.
//   - Backup datado antes de sobrescrever, podado nas N ultimas revisoes.
//   - Escrita atomica (tmp + rename).
//   - Toda gravacao vai para o log de auditoria.
// =============================================================================

declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');
header('Cache-Control: no-store');

const MAX_BYTES      = 1048576;             // 1 MB por arquivo
const BACKUP_DIRNAME = '.inventory-backups';
const KEEP_REVISIONS = 10;                  // revisoes mantidas por arquivo
// Estado do utilitario. Fica DENTRO de ~/.claude de proposito: o pool tem
// open_basedir restrito, e ~/.claude e uma das raizes liberadas. Arquivo
// fora dela e invisivel para o backend — foi assim que o log de auditoria
// falhou em silencio ate 08/08/2026.
const STATE_DIR      = '/.claude/.inventory';
const AUDIT_FILE     = STATE_DIR . '/audit.log';
const STATUS_FILE    = STATE_DIR . '/status.json';
const REQUEST_FILE   = STATE_DIR . '/regen.request';
// Hash da senha. Dentro de STATE_DIR (diretorio liberado no open_basedir),
// e nao solto no home: o open_basedir lista arquivo avulso como entrada
// propria, e criar o temporario ao lado dele para a escrita atomica falha.
// Texto puro, NAO codigo PHP: `require` de um .php passa pelo opcache, que
// serve a versao anterior por ate opcache.revalidate_freq segundos depois da
// troca — a senha velha continuava valendo e a nova era recusada.
const AUTH_HASH      = STATE_DIR . '/auth.hash';
const AUTH_FILE      = STATE_DIR . '/auth.php';
const AUTH_LEGACY    = '/.claude-inventory-auth.php';

/** Resposta JSON e encerramento. */
function out(int $code, array $payload): never {
    http_response_code($code);
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function home(): string {
    $h = getenv('HOME') ?: '';
    if ($h === '') {
        out(500, ['error' => 'HOME nao resolvido no pool PHP-FPM.']);
    }
    return $h;
}

/** Diretorio ~/.claude do usuario que roda este pool. */
function claude_base(): string {
    $base = realpath(home() . '/.claude');
    if ($base === false) {
        out(500, ['error' => 'Diretorio ~/.claude nao encontrado.']);
    }
    return $base;
}

/** Valida a forma do caminho relativo, sem exigir que o arquivo exista. */
function check_shape(string $rel): string {
    if ($rel === '' || str_contains($rel, "\0") || str_starts_with($rel, '/')
        || str_contains($rel, '..')) {
        out(400, ['error' => 'Caminho invalido.']);
    }
    if (!str_ends_with(strtolower($rel), '.md')) {
        out(403, ['error' => 'Apenas arquivos .md sao editaveis.']);
    }
    $first = explode('/', $rel)[0];
    if (!in_array($first, ['skills', 'agents', 'commands'], true)) {
        out(403, ['error' => 'Fora do escopo editavel (skills, agents, commands).', 'code' => 'escopo']);
    }
    return $first;
}

/** Caminho absoluto de um arquivo que DEVE existir. */
function resolve_target(string $rel): string {
    $base  = claude_base();
    $first = check_shape($rel);

    $abs = realpath($base . '/' . $rel);
    if ($abs === false || !is_file($abs)) {
        out(404, ['error' => 'Arquivo nao encontrado.', 'code' => 'naoexiste']);
    }
    $root = realpath($base . '/' . $first);
    if ($root === false || !str_starts_with($abs, $root . DIRECTORY_SEPARATOR)) {
        out(403, ['error' => 'Caminho resolvido fora do escopo permitido.']);
    }
    return $abs;
}

/** Confere a senha de gravacao contra o hash guardado fora do webroot. */
/**
 * Hash em vigor. Prefere o arquivo de dados; cai nos formatos .php antigos
 * apenas enquanto uma instalacao nao tiver migrado.
 */
function current_hash(): string {
    $plain = home() . AUTH_HASH;
    if (is_file($plain)) {
        return trim((string) file_get_contents($plain));
    }
    foreach ([home() . AUTH_FILE, home() . AUTH_LEGACY] as $legacy) {
        if (is_file($legacy)) {
            $h = require $legacy;
            return is_string($h) ? $h : '';
        }
    }
    return '';
}

function check_password(string $sent): void {
    $hash = current_hash();
    if ($hash === '') {
        out(500, ['error' => 'Senha de gravacao nao configurada no servidor.']);
    }
    if ($sent === '' || !password_verify($sent, $hash)) {
        usleep(400000);                     // atrasa tentativa em massa
        audit('senha-recusada', '', 0);
        out(401, ['error' => 'Senha incorreta.', 'code' => 'senha']);
    }
}

// ---------------------------------------------------------------------------
// Validacao de frontmatter
// ---------------------------------------------------------------------------
/**
 * Skill e agent sem `name` e `description` no frontmatter deixam de ser
 * carregados pelo Claude Code — e a falha e silenciosa, so aparece quando
 * voce vai usar. Por isso o save e recusado aqui, nao apenas avisado.
 * Command nao exige frontmatter (o formato aceita arquivo puro).
 */
function validate_frontmatter(string $rel, string $content): void {
    $kind = explode('/', $rel)[0];
    if ($kind === 'commands') {
        return;
    }

    if (!preg_match('/^---\s*\R(.*?)\R---\s*(\R|$)/s', $content, $m)) {
        out(422, ['error' => 'Frontmatter ausente. O arquivo precisa comecar com um bloco --- ... --- contendo name e description.', 'code' => 'fm_ausente']);
    }
    $yaml = $m[1];
    $faltando = [];
    foreach (['name', 'description'] as $key) {
        if (!preg_match('/^' . $key . ':\s*(\S.*)$/mi', $yaml)) {
            $faltando[] = $key;
        }
    }
    if ($faltando) {
        out(422, ['error' => 'Frontmatter incompleto: falta ' . implode(' e ', $faltando)
                             . '. Sem esse campo o Claude Code nao carrega o recurso.',
                  'code' => 'fm_falta', 'faltando' => $faltando]);
    }
}

// ---------------------------------------------------------------------------
// Revisoes (.bak) e auditoria
// ---------------------------------------------------------------------------
function backup_dir(string $abs): string {
    return dirname($abs) . '/' . BACKUP_DIRNAME;
}

/** Grava backup datado do conteudo atual e poda os excedentes. */
function backup(string $abs): void {
    $dir = backup_dir($abs);
    if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
        return;                             // backup e defesa extra, nao bloqueia
    }
    @copy($abs, $dir . '/' . basename($abs) . '.' . date('Ymd-His') . '.bak');
    prune($abs);
}

/** Mantem apenas as KEEP_REVISIONS revisoes mais recentes do arquivo. */
function prune(string $abs): void {
    $revs = revisions($abs);
    foreach (array_slice($revs, KEEP_REVISIONS) as $r) {
        @unlink(backup_dir($abs) . '/' . $r['rev']);
    }
}

/** Revisoes existentes, da mais recente para a mais antiga. */
function revisions(string $abs): array {
    $dir = backup_dir($abs);
    if (!is_dir($dir)) {
        return [];
    }
    $prefix = basename($abs) . '.';
    $out = [];
    foreach ((array) scandir($dir) as $f) {
        if (!is_string($f) || !str_starts_with($f, $prefix) || !str_ends_with($f, '.bak')) {
            continue;
        }
        $full = $dir . '/' . $f;
        if (!is_file($full)) {
            continue;
        }
        $out[] = ['rev' => $f, 'mtime' => filemtime($full), 'bytes' => filesize($full)];
    }
    usort($out, fn($a, $b) => $b['mtime'] <=> $a['mtime']);
    return $out;
}

/** Caminho absoluto de uma revisao, validando o nome contra a lista real. */
function resolve_revision(string $abs, string $rev): string {
    if (str_contains($rev, '/') || str_contains($rev, "\0")) {
        out(400, ['error' => 'Revisao invalida.']);
    }
    foreach (revisions($abs) as $r) {
        if ($r['rev'] === $rev) {
            return backup_dir($abs) . '/' . $rev;
        }
    }
    out(404, ['error' => 'Revisao nao encontrada.']);
}

/**
 * Registro de auditoria em JSONL. Escrita numa pasta que o Claude Code
 * executa merece rastro: quem, o que, quando.
 */
function audit(string $action, string $file, int $bytes, array $extra = []): void {
    $line = json_encode(array_merge([
        'ts'     => date('c'),
        'action' => $action,
        'file'   => $file,
        'bytes'  => $bytes,
        'ip'     => $_SERVER['REMOTE_ADDR'] ?? '?',
        'ua'     => substr((string) ($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 120),
    ], $extra), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    $dir = home() . STATE_DIR;
    if (!is_dir($dir)) {
        @mkdir($dir, 0755, true);
    }
    @file_put_contents(home() . AUDIT_FILE, $line . "\n", FILE_APPEND | LOCK_EX);
}

/** Escrita atomica: temporario no mesmo diretorio, depois rename. */
function write_atomic(string $abs, string $content): void {
    $tmp = $abs . '.tmp-' . bin2hex(random_bytes(6));
    if (file_put_contents($tmp, $content) === false || !rename($tmp, $abs)) {
        @unlink($tmp);
        out(500, ['error' => 'Falha ao gravar o arquivo.']);
    }
    clearstatcache(true, $abs);
}

// ---------------------------------------------------------------------------
// Roteamento
// ---------------------------------------------------------------------------
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    out(405, ['error' => 'Use POST.']);
}

// Sem offset/maxlen: em stream nao-buscavel (pipe) essa forma falha em
// silencio e o corpo chega vazio. O tamanho e conferido logo abaixo.
$raw = (string) file_get_contents('php://input');
if (strlen($raw) > MAX_BYTES + 4096) {
    out(413, ['error' => 'Requisicao acima do limite.']);
}
$req = json_decode($raw, true);
if (!is_array($req)) {
    out(400, ['error' => 'JSON invalido.']);
}

$action = (string) ($req['action'] ?? '');
$file   = (string) ($req['file'] ?? '');

switch ($action) {

case 'read':
    $abs = resolve_target($file);
    $content = file_get_contents($abs);
    if ($content === false) {
        out(500, ['error' => 'Falha ao ler o arquivo.']);
    }
    out(200, [
        'file'      => $file,
        'content'   => $content,
        'mtime'     => filemtime($abs),
        'writable'  => is_writable($abs),
        'revisions' => count(revisions($abs)),
    ]);

case 'history':
    $abs = resolve_target($file);
    out(200, ['file' => $file, 'revisions' => revisions($abs), 'keep' => KEEP_REVISIONS]);

case 'revision':
    $abs = resolve_target($file);
    $path = resolve_revision($abs, (string) ($req['rev'] ?? ''));
    out(200, ['file' => $file, 'rev' => $req['rev'], 'content' => (string) file_get_contents($path)]);

case 'save':
    check_password((string) ($req['password'] ?? ''));
    $abs = resolve_target($file);
    $content = (string) ($req['content'] ?? '');

    if (strlen($content) > MAX_BYTES) {
        out(413, ['error' => 'Conteudo acima de 1 MB.']);
    }
    if (!is_writable($abs)) {
        out(403, ['error' => 'Arquivo sem permissao de escrita para o pool.']);
    }
    // O browser manda o mtime que leu: detecta edicao concorrente.
    $known = $req['mtime'] ?? null;
    if (is_int($known) && filemtime($abs) !== $known) {
        out(409, ['error' => 'O arquivo mudou no disco desde que voce abriu. Recarregue antes de salvar.', 'code' => 'conflito']);
    }
    validate_frontmatter($file, $content);

    backup($abs);
    write_atomic($abs, $content);
    audit('save', $file, strlen($content));
    out(200, ['ok' => true, 'file' => $file, 'mtime' => filemtime($abs),
              'bytes' => strlen($content), 'revisions' => count(revisions($abs))]);

case 'create':
    check_password((string) ($req['password'] ?? ''));
    $first = check_shape($file);
    $content = (string) ($req['content'] ?? '');

    if (strlen($content) > MAX_BYTES) {
        out(413, ['error' => 'Conteudo acima de 1 MB.']);
    }
    // Diretorios tem de ser slugs simples; o arquivo segue a convencao do
    // tipo — skill e sempre SKILL.md, os demais sao <slug>.md.
    $segs = explode('/', $file);
    $leaf = array_pop($segs);

    foreach ($segs as $seg) {
        if (!preg_match('/^[a-z0-9][a-z0-9._-]*$/', $seg)) {
            out(400, ['error' => "Nome de pasta invalido: \"$seg\". Use minusculas, numeros e hifen."]);
        }
    }
    if ($first === 'skills') {
        if ($leaf !== 'SKILL.md') {
            out(400, ['error' => 'Skill precisa terminar em <nome>/SKILL.md.']);
        }
        if (count($segs) !== 2) {          // skills/<nome>
            out(400, ['error' => 'Skill vai em skills/<nome>/SKILL.md.']);
        }
    } elseif (!preg_match('/^[a-z0-9][a-z0-9._-]*\.md$/', $leaf)) {
        out(400, ['error' => "Nome invalido: \"$leaf\". Use minusculas, numeros e hifen."]);
    }
    $base = claude_base();
    $abs  = $base . '/' . $file;
    if (file_exists($abs)) {
        out(409, ['error' => 'Ja existe um arquivo nesse caminho.', 'code' => 'existe']);
    }
    validate_frontmatter($file, $content);

    $dir = dirname($abs);
    if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
        out(500, ['error' => 'Falha ao criar o diretorio.']);
    }
    // Confere que o diretorio criado ficou mesmo sob a raiz permitida.
    $root = realpath($base . '/' . $first);
    if ($root === false || !str_starts_with((string) realpath($dir), $root)) {
        out(403, ['error' => 'Caminho resolvido fora do escopo permitido.']);
    }
    write_atomic($abs, $content);
    audit('create', $file, strlen($content));
    out(200, ['ok' => true, 'file' => $file, 'mtime' => filemtime($abs), 'bytes' => strlen($content)]);

case 'passwd':
    // Troca a senha de gravacao. Exige a senha atual — nao ha recuperacao
    // por aqui de proposito: quem perdeu a senha tem acesso ao servidor e
    // reescreve o hash direto no arquivo.
    check_password((string) ($req['password'] ?? ''));

    $nova = (string) ($req['nova'] ?? '');
    // Sem mb_strlen de proposito: a extensao mbstring nao esta instalada
    // em todo servidor. preg com /u conta caracteres; se a senha nao for
    // UTF-8 valido, cai no tamanho em bytes.
    $len = preg_match_all('/./u', $nova);
    if ($len === false) {
        $len = strlen($nova);
    }
    if ($len < 8) {
        out(422, ['error' => 'A senha nova precisa de pelo menos 8 caracteres.']);
    }
    if ($nova === (string) ($req['password'] ?? '')) {
        out(422, ['error' => 'A senha nova e igual a atual.']);
    }

    $dir = home() . STATE_DIR;
    if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
        out(500, ['error' => 'Nao foi possivel criar o diretorio de estado.']);
    }
    $authFile = home() . AUTH_HASH;    // troca sempre grava no formato novo
    $body = password_hash($nova, PASSWORD_BCRYPT) . "\n";

    // Escrita atomica no mesmo diretorio, para nao deixar o arquivo pela
    // metade e travar o acesso.
    $tmp = $authFile . '.tmp-' . bin2hex(random_bytes(6));
    if (file_put_contents($tmp, $body) === false || !@chmod($tmp, 0600) || !rename($tmp, $authFile)) {
        @unlink($tmp);
        out(500, ['error' => 'Falha ao gravar a senha nova.']);
    }
    // Some com os formatos antigos para nao restar senha valida esquecida.
    foreach ([home() . AUTH_FILE, home() . AUTH_LEGACY] as $legacy) {
        if (is_file($legacy)) {
            @unlink($legacy);
        }
    }
    audit('senha-trocada', '', 0);
    out(200, ['ok' => true, 'msg' => 'Senha trocada.']);

case 'status':
    // Estado da ultima regeneracao + se ha pedido na fila.
    $st = @file_get_contents(home() . STATUS_FILE);
    $data = $st !== false ? json_decode($st, true) : null;
    out(200, [
        'status'  => is_array($data) ? $data : null,
        'pending' => is_file(home() . REQUEST_FILE),
    ]);

case 'regen':
    // Este pool roda com exec/system/shell_exec desabilitados de proposito.
    // Em vez de afrouxar isso, deixamos um arquivo de pedido: o cron que
    // roda de minuto em minuto o consome e regenera. O custo e a espera de
    // ate 60s; o ganho e o backend web seguir sem poder executar processo.
    check_password((string) ($req['password'] ?? ''));
    $dir = home() . STATE_DIR;
    if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
        out(500, ['error' => 'Nao foi possivel criar o diretorio de estado.']);
    }
    if (@file_put_contents(home() . REQUEST_FILE, date('c') . "\n") === false) {
        out(500, ['error' => 'Nao foi possivel registrar o pedido.']);
    }
    audit('regen-pedido', '', 0);
    out(200, ['ok' => true, 'queued' => true,
              'msg' => 'Pedido registrado. A regeneracao roda em ate 60 segundos.']);

case 'restore':
    check_password((string) ($req['password'] ?? ''));
    $abs  = resolve_target($file);
    $rev  = (string) ($req['rev'] ?? '');
    $path = resolve_revision($abs, $rev);
    $content = (string) file_get_contents($path);

    validate_frontmatter($file, $content);
    backup($abs);                            // o estado atual tambem vira revisao
    write_atomic($abs, $content);
    audit('restore', $file, strlen($content), ['rev' => $rev]);
    out(200, ['ok' => true, 'file' => $file, 'mtime' => filemtime($abs),
              'bytes' => strlen($content), 'content' => $content]);
}

out(400, ['error' => 'Acao desconhecida.']);
