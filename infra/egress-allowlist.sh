#!/usr/bin/env bash
#
# egress-allowlist.sh — ограничение ИСХОДЯЩИХ соединений сервера бота.
#
# ЗАЧЕМ. Бот ходит в интернет только в несколько мест: серверы MAX, DNS,
# сверка времени. Если на сервер попадёт вредоносный код (например,
# закладка в сторонней библиотеке), ему нужен канал, чтобы вытащить
# данные наружу. Пока сервер может соединяться куда угодно — такой канал
# открыт. Этот скрипт закрывает всё исходящее, КРОМЕ явно разрешённого.
# Для модели угроз это снимает целый класс угроз (утечка через egress).
#
# ЧЕГО СКРИПТ НЕ ДЕЛАЕТ (важно — он безопасен для доступа к серверу):
#   * НЕ трогает входящие соединения. Ваш SSH-доступ не пострадает.
#   * НЕ рвёт уже открытые соединения (established) — включение на живом
#     сервере не обрывает текущий сеанс и текущую работу бота.
#   * НЕ включает блокировку, если список разрешённых адресов пуст
#     (например, не сработал DNS) — чтобы не отрезать бота от MAX.
#   * Откатывается одной командой: sudo ./egress-allowlist.sh disable
#
# КАК ПОЛЬЗОВАТЬСЯ (от root или через sudo):
#   sudo ./egress-allowlist.sh test      # показать, что разрешится, НИЧЕГО не меняя
#   sudo ./egress-allowlist.sh enable     # включить ограничение
#   sudo ./egress-allowlist.sh status     # показать текущее состояние
#   sudo ./egress-allowlist.sh refresh    # обновить список адресов (для cron)
#   sudo ./egress-allowlist.sh disable    # ПОЛНОСТЬЮ выключить, вернуть как было
#
# ПОСЛЕ enable ОБЯЗАТЕЛЬНО проверьте, что бот жив: напишите ему в MAX или
#   curl -fsS http://127.0.0.1:8080/livez && echo OK
# Если что-то не так — сразу: sudo ./egress-allowlist.sh disable
#
# АДРЕСА MAX могут менять IP (это один кластер под несколькими именами),
# поэтому список наполняется по ИМЕНАМ и обновляется по расписанию. Ставьте
# refresh в cron root каждые 15 минут (см. вику, раздел про egress).

set -euo pipefail

# --- ЧТО РАЗРЕШЕНО -----------------------------------------------------
#
# Домены, к которым РАЗРЕШЁН исходящий HTTPS (443): рантайм бота (MAX,
# антифишинг-фиды) + инфраструктура обновлений (Debian, PyPI/uv, Docker,
# GitHub) — чтобы сервер можно было патчить и обновлять зависимости, не
# снимая allowlist. Решение владельца 2026-07-24.
#
# ВАЖНО про CDN. deb.debian.org, pypi.org, *.docker.io, github.com отдают
# МНОГО быстро сменяющихся IP (Fastly/Cloudflare). Скрипт резолвит домены
# в IP на момент `refresh`/`enable`; между рефрешами IP у CDN могут
# уехать. Поэтому:
#   • держите `refresh` в cron (см. вику), И
#   • перед `apt update` / `uv sync` / `docker pull` выполните
#     `sudo ./egress-allowlist.sh refresh` вручную,
#   • либо на окно обслуживания `disable`, обновитесь, затем `enable`.
# Модель угроз: allowlist по-прежнему закрывает произвольный exfil на
# хосты атакующего; открыты только доверенные MAX + пакетная
# инфраструктура — это осознанный компромисс (патчи важнее сужения).
ALLOWED_DOMAINS=(
    # --- MAX Bot API (обязательно, рантайм бота) ---
    "platform-api2.max.ru"
    "platform-api.max.ru"   # старое имя, до полного вывода из эксплуатации

    # --- Обновления ОС Debian (apt) ---
    # Нужны, чтобы сервер можно было патчить, не снимая allowlist.
    "deb.debian.org"
    "security.debian.org"
    "ftp.debian.org"

    # --- Обновления зависимостей Python (uv / pip) ---
    "pypi.org"
    "files.pythonhosted.org"
    "astral.sh"                 # установщик и релизы uv

    # --- Обновление образов контейнеров (docker/podman pull) ---
    "registry-1.docker.io"
    "index.docker.io"
    "auth.docker.io"
    "production.cloudflare.docker.com"
    "ghcr.io"                   # если базовые образы с GitHub Container Registry
    "pkg-containers.githubusercontent.com"

    # --- GitHub (исходники зависимостей, автодеплой, actions) ---
    "github.com"
    "api.github.com"
    "codeload.github.com"
    "objects.githubusercontent.com"
    "raw.githubusercontent.com"

    # --- Антифишинг-фиды (рантайм: проверка ссылок жителей) ---
    "threatfox.abuse.ch"
    "urlhaus.abuse.ch"
    "data.phishtank.com"
)

# Порты, разрешённые к адресам из списка выше.
ALLOWED_HTTPS_PORT=443

# Внутренние подсети Docker/Podman (bot↔db внутри сервера). Не трогаем —
# иначе бот потеряет базу. Покрывают стандартные диапазоны bridge-сетей.
DOCKER_SUBNETS=("172.16.0.0/12" "10.0.0.0/8" "192.168.0.0/16")

# Имя набора адресов в ipset.
IPSET_NAME="aemr_egress_allow"
# Пометка правил, чтобы находить и снимать именно свои.
CHAIN_TAG="AEMR-EGRESS"

# ----------------------------------------------------------------------

log() { printf '%s\n' "$*" >&2; }
die() { log "ОШИБКА: $*"; exit 1; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "нужны права root. Запускайте через sudo."
    fi
}

require_tools() {
    for t in ipset iptables getent; do
        command -v "$t" >/dev/null 2>&1 || die "не найдена утилита '$t'. Установите: apt-get install ipset iptables"
    done
}

# Разрешить резолв доменов в IP. Возвращает список IPv4 через перевод строки.
resolve_domains() {
    local domain ip found
    for domain in "${ALLOWED_DOMAINS[@]}"; do
        # getent hosts даёт и IPv4, и IPv6; берём только IPv4.
        found=0
        while read -r ip _; do
            case "$ip" in
                *.*.*.*)
                    printf '%s\n' "$ip"
                    found=1
                    ;;
            esac
        done < <(getent ahostsv4 "$domain" 2>/dev/null || true)
        if [ "$found" -eq 0 ]; then
            log "ПРЕДУПРЕЖДЕНИЕ: не удалось разрезолвить $domain — пропущен в этот раз"
        fi
    done
}

ensure_ipset() {
    if ! ipset list "$IPSET_NAME" >/dev/null 2>&1; then
        ipset create "$IPSET_NAME" hash:ip family inet timeout 0
    fi
}

# Наполнить ipset свежими адресами. Fail-open: если ничего не
# разрезолвилось, СТАРЫЙ набор НЕ трогаем (иначе отрежем бота от MAX).
populate_ipset() {
    local ips
    ips="$(resolve_domains | sort -u)"
    if [ -z "$ips" ]; then
        log "резолв не дал ни одного адреса — оставляю прежний список без изменений"
        return 1
    fi
    ensure_ipset
    # Новый временный набор, затем атомарный swap — без окна пустого списка.
    local tmp="${IPSET_NAME}_tmp"
    ipset destroy "$tmp" 2>/dev/null || true
    ipset create "$tmp" hash:ip family inet timeout 0
    local ip
    while read -r ip; do
        if [ -n "$ip" ]; then
            ipset add "$tmp" "$ip" 2>/dev/null || true
        fi
    done <<< "$ips"
    ensure_ipset
    ipset swap "$tmp" "$IPSET_NAME"
    ipset destroy "$tmp" 2>/dev/null || true
    local n
    n="$(ipset list "$IPSET_NAME" | grep -c '^[0-9]' || true)"
    log "разрешённых адресов в наборе: $n"
    return 0
}

# Построить правила в одной цепочке (OUTPUT для хоста, DOCKER-USER для
# контейнеров). $1 — имя цепочки.
apply_rules_to_chain() {
    local chain="$1"
    # established/related — первым: не рвём текущие соединения.
    iptables -I "$chain" 1 -m conntrack --ctstate ESTABLISHED,RELATED \
        -m comment --comment "$CHAIN_TAG" -j ACCEPT
    # loopback.
    iptables -I "$chain" 2 -o lo -m comment --comment "$CHAIN_TAG" -j ACCEPT
    iptables -I "$chain" 3 -d 127.0.0.0/8 -m comment --comment "$CHAIN_TAG" -j ACCEPT
    # Внутренние сети Docker/Podman (bot↔db).
    local net idx=4
    for net in "${DOCKER_SUBNETS[@]}"; do
        iptables -I "$chain" "$idx" -d "$net" -m comment --comment "$CHAIN_TAG" -j ACCEPT
        idx=$((idx + 1))
    done
    # DNS (резолв имён) и NTP (сверка времени) — без них TLS и резолв MAX
    # сломаются. Разрешаем широко: это служебный трафик, не канал утечки.
    iptables -I "$chain" "$idx" -p udp --dport 53 -m comment --comment "$CHAIN_TAG" -j ACCEPT; idx=$((idx+1))
    iptables -I "$chain" "$idx" -p tcp --dport 53 -m comment --comment "$CHAIN_TAG" -j ACCEPT; idx=$((idx+1))
    iptables -I "$chain" "$idx" -p udp --dport 123 -m comment --comment "$CHAIN_TAG" -j ACCEPT; idx=$((idx+1))
    # HTTPS только к разрешённым адресам (MAX и опциональные).
    iptables -I "$chain" "$idx" -p tcp --dport "$ALLOWED_HTTPS_PORT" \
        -m set --match-set "$IPSET_NAME" dst \
        -m comment --comment "$CHAIN_TAG" -j ACCEPT; idx=$((idx+1))
    # Всё остальное исходящее (новое) — запретить, с журналированием.
    #
    # ВАЖНО: используем -I (вставку) на позицию idx, а НЕ -A (в конец).
    # В цепочке DOCKER-USER в конце стоит правило `-j RETURN` (Docker его
    # ставит сам). Если добавить DROP в конец через -A, он окажется ПОСЛЕ
    # RETURN и никогда не сработает — контейнерный трафик остался бы
    # открытым, а мы бы думали, что закрыли. Вставка на idx ставит DROP
    # сразу после наших ACCEPT-правил и ДО RETURN.
    if iptables -I "$chain" "$idx" -m conntrack --ctstate NEW \
        -m comment --comment "$CHAIN_TAG-DROP" \
        -j LOG --log-prefix "aemr-egress-drop: " --log-level 4 2>/dev/null; then
        idx=$((idx + 1))
    fi
    iptables -I "$chain" "$idx" -m conntrack --ctstate NEW \
        -m comment --comment "$CHAIN_TAG" -j DROP
}

# Снять все свои правила из цепочки по метке.
remove_rules_from_chain() {
    local chain="$1"
    # Удаляем по совпадению комментария, пока такие правила есть.
    while iptables -S "$chain" 2>/dev/null | grep -q -- "$CHAIN_TAG"; do
        local line
        line="$(iptables -S "$chain" | grep -n -- "$CHAIN_TAG" | head -1 | cut -d: -f1)"
        # -S нумерует с 1 включая политику (-P) первой строкой, поэтому -1.
        iptables -D "$chain" "$((line - 1))" 2>/dev/null || break
    done
}

ensure_docker_user_chain() {
    # DOCKER-USER существует, только если Docker поднят. Если её нет —
    # значит контейнерный трафик через FORWARD не фильтруется отдельно;
    # это не ошибка (Podman/rootless), просто пропускаем.
    iptables -S DOCKER-USER >/dev/null 2>&1
}

cmd_test() {
    require_tools
    log "=== РЕЖИМ ПРОВЕРКИ (ничего не меняется) ==="
    log "Разрешённые домены и их текущие адреса:"
    local ips
    ips="$(resolve_domains | sort -u)"
    if [ -z "$ips" ]; then
        die "не удалось разрезолвить НИ ОДНОГО домена — не включайте enable, сначала почините DNS"
    fi
    printf '%s\n' "$ips" | sed 's/^/    /' >&2
    log "Также будут разрешены: loopback, внутренние сети Docker, DNS(53), NTP(123), ответы на исходящие."
    log "Всё прочее исходящее будет запрещено."
    log "Если список выше выглядит верно — включайте: sudo $0 enable"
}

cmd_enable() {
    require_root; require_tools
    log "Наполняю список разрешённых адресов..."
    populate_ipset || die "список адресов пуст — включение отменено, чтобы не отрезать бота от MAX"
    # На всякий случай снимем прежние свои правила (повторный enable).
    remove_rules_from_chain OUTPUT
    if ensure_docker_user_chain; then remove_rules_from_chain DOCKER-USER; fi
    log "Ставлю правила на исходящий трафик хоста (OUTPUT)..."
    apply_rules_to_chain OUTPUT
    if ensure_docker_user_chain; then
        log "Ставлю правила на исходящий трафик контейнеров (DOCKER-USER)..."
        apply_rules_to_chain DOCKER-USER
    else
        log "Цепочка DOCKER-USER не найдена (Docker не запущен или Podman) — фильтрую только хостовый трафик."
    fi
    log ""
    log "ГОТОВО. Ограничение включено."
    log "ПРОВЕРЬТЕ, что бот жив: curl -fsS http://127.0.0.1:8080/livez && echo OK"
    log "Если бот не отвечает — откат: sudo $0 disable"
    log "Не забудьте поставить refresh в cron (см. вику)."
}

cmd_refresh() {
    require_root; require_tools
    if ! iptables -S OUTPUT 2>/dev/null | grep -q -- "$CHAIN_TAG"; then
        log "ограничение сейчас выключено — refresh пропущен"
        exit 0
    fi
    populate_ipset || log "refresh: список не обновлён (резолв не удался), действует прежний"
}

cmd_status() {
    require_tools
    if iptables -S OUTPUT 2>/dev/null | grep -q -- "$CHAIN_TAG"; then
        log "СОСТОЯНИЕ: ограничение исходящего ВКЛЮЧЕНО."
    else
        log "СОСТОЯНИЕ: ограничение исходящего ВЫКЛЮЧЕНО."
    fi
    if ipset list "$IPSET_NAME" >/dev/null 2>&1; then
        local n
        n="$(ipset list "$IPSET_NAME" | grep -c '^[0-9]' || true)"
        log "Разрешённых адресов в наборе: $n"
        ipset list "$IPSET_NAME" | grep '^[0-9]' | sed 's/^/    /' >&2 || true
    fi
    log "Правила в OUTPUT:"
    iptables -S OUTPUT | grep -- "$CHAIN_TAG" | sed 's/^/    /' >&2 || log "    (нет)"
}

cmd_disable() {
    require_root; require_tools
    log "Снимаю правила ограничения..."
    remove_rules_from_chain OUTPUT
    if ensure_docker_user_chain; then remove_rules_from_chain DOCKER-USER; fi
    ipset destroy "$IPSET_NAME" 2>/dev/null || true
    log "ГОТОВО. Исходящий трафик снова не ограничен (как было до enable)."
}

case "${1:-}" in
    test)    cmd_test ;;
    enable)  cmd_enable ;;
    refresh) cmd_refresh ;;
    status)  cmd_status ;;
    disable) cmd_disable ;;
    *)
        log "Использование: $0 {test|enable|status|refresh|disable}"
        log "Начните с 'test' — он ничего не меняет, только показывает, что будет разрешено."
        exit 1
        ;;
esac
