"""Unit tests for `hostlens.core.redact.redact_text`.

Covers the five rule classes defined in OPERABILITY.md §7.2:
1. `password=...` keyword assignment
2. `secret=...` keyword assignment
3. `token=...` / `api_key=...` / `bearer ...` keyword assignment
4. JWT three-segment tokens (`eyJ...`)
5. Anthropic / OpenAI `sk-...` keys
"""

from __future__ import annotations

import pytest

from hostlens.core.redact import redact_text


class TestKeywordAssignment:
    def test_password_equals_is_masked(self) -> None:
        out = redact_text("password=p@ssw0rd!supersecret")
        assert "p@ssw0rd!supersecret" not in out
        assert "password=" in out
        assert "..." in out

    def test_password_colon_with_spaces_is_masked(self) -> None:
        out = redact_text("password : verylongpassword123")
        assert "verylongpassword123" not in out

    def test_secret_assignment_is_masked(self) -> None:
        out = redact_text("secret=topsecretvalue")
        assert "topsecretvalue" not in out

    def test_secret_colon_is_masked(self) -> None:
        out = redact_text("secret: anotherlongsecret")
        assert "anotherlongsecret" not in out

    def test_token_is_masked(self) -> None:
        out = redact_text("token=ghp_1234567890abcdefghij")
        assert "ghp_1234567890abcdefghij" not in out

    def test_api_key_underscore_is_masked(self) -> None:
        out = redact_text("api_key=longvaluexyz123")
        assert "longvaluexyz123" not in out

    def test_api_key_hyphen_is_masked(self) -> None:
        out = redact_text("api-key=somelongvalue999")
        assert "somelongvalue999" not in out

    def test_bearer_is_masked(self) -> None:
        out = redact_text("bearer=mytokenvalue123456")
        assert "mytokenvalue123456" not in out

    def test_case_insensitive_keyword(self) -> None:
        out = redact_text("PASSWORD=mysecretvalue999")
        assert "mysecretvalue999" not in out

    def test_short_value_fully_masked(self) -> None:
        # value <=8 chars masked as ****
        out = redact_text("password=short")
        assert "short" not in out
        assert "****" in out

    def test_quoted_value_with_spaces_no_tail_leak(self) -> None:
        # `password="a b"` — a bare `(\S+)` truncates at the in-quote space and
        # leaks the tail (`password=**** def"`); the quote-aware value run masks
        # the whole quoted span.
        out = redact_text('password="abc def"')
        assert "def" not in out
        assert redact_text(out) == out  # idempotent


class TestJWT:
    def test_simple_jwt_is_masked(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = redact_text(f"Authorization: Bearer {jwt}")
        assert jwt not in out
        assert "eyJh" in out  # first 4 retained
        assert "..." in out

    def test_jwt_in_log_line(self) -> None:
        jwt = "eyJ0eXAiOiJKV1Q.eyJleHAiOjE2MDB9.abcDEF1234"
        out = redact_text(f"got token={jwt} from upstream")
        assert jwt not in out

    def test_bearer_no_space_token_unchanged(self) -> None:
        # A real base64url bearer token has no space; output is byte-identical
        # to the pre-quote-aware `\\S+` form.
        out = redact_text("Authorization: Bearer eyJabcdefghijklmnopqrstuvwxyz")
        assert out == "Authorization: Bearer eyJa...wxyz"

    def test_bearer_quoted_value_with_spaces_no_tail_leak(self) -> None:
        # `Bearer "a b c"` — a bare `(\\S+)` truncated at the in-quote space and
        # masked only `"a`, leaking ` b c"`. The quote-aware value masks whole.
        out = redact_text('Authorization: Bearer "a b c"')
        assert "b c" not in out
        assert out == "Authorization: Bearer ****"
        assert redact_text(out) == out  # idempotent


class TestSkKey:
    def test_anthropic_sk_key_masked(self) -> None:
        key = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
        out = redact_text(f"key is {key}")
        assert key not in out
        assert "sk-a" in out  # prefix retained

    def test_openai_sk_with_hyphen_masked(self) -> None:
        key = "sk-proj-1234567890abcdefghijklmn"
        out = redact_text(key)
        assert key not in out


class TestNoSecret:
    def test_plain_text_unchanged(self) -> None:
        assert redact_text("hello world") == "hello world"

    def test_short_sk_prefix_not_matched(self) -> None:
        # Less than 20 chars after `sk-` → not matched
        assert redact_text("sk-short") == "sk-short"

    def test_empty_string(self) -> None:
        assert redact_text("") == ""

    def test_long_non_url_blob_no_catastrophic_backtracking(self) -> None:
        # A long unbroken alphanumeric token (cert dump / base64 / log blob)
        # must not trigger O(n^2) backtracking in the URL regexes — redact_text
        # runs on every render boundary. Bounded scheme run keeps it linear.
        import time

        blob = "X" * 64000
        start = time.perf_counter()
        out = redact_text(blob)
        assert time.perf_counter() - start < 2.0  # was ~17-36s before the bound
        assert out == blob  # nothing to redact

    def test_long_escaped_quote_run_no_catastrophic_backtracking(self) -> None:
        # A long escaped-quote run (`"` + `\"`*n — e.g. a truncated curl JSON
        # error body in evidence.stdout / transport_error) must not trigger
        # O(n^2) in the shell-word / env-credential regexes. The single
        # optional-close quote alternative (no closed/unterminated overlap)
        # keeps it linear; doubling the input must not ~4x the time.
        import time

        def elapsed(n: int) -> float:
            s = 'mysql -p"' + '\\"' * n
            start = time.perf_counter()
            redact_text(s)
            return time.perf_counter() - start

        # 32k escaped quotes was ~11s with the overlapping-alternation regex.
        assert elapsed(32000) < 2.0
        assert redact_text('PGPASSWORD="' + '\\"' * 32000) is not None  # env path too


class TestPrefixSuffixPreservation:
    def test_mask_preserves_4_4(self) -> None:
        key = "sk-abcdefghijklmnopqrst7890"
        out = redact_text(key)
        # Format: <first4>...<last4>
        assert out.startswith("sk-a")
        assert out.endswith("7890")
        assert "..." in out


@pytest.mark.parametrize(
    "line",
    [
        "password=verylongpasswordhere",
        "secret=anothersecretvalue",
        "token=ghp_xxxxxxxxxxxxxxxxxx",
        "api_key=somekeyvalue1234567",
        "bearer=jwtlikevalueabcdef",
    ],
)
def test_assignment_keeps_keyword_visible(line: str) -> None:
    """Keyword stays in output (only the value is masked)."""
    out = redact_text(line)
    keyword = line.split("=", 1)[0]
    assert keyword in out


# --------------------------------------------------------------------------- #
# [A] Space-separated long flag (`--password <value>` etc.)
#     spec: 需求:`redact_text` 必须脱敏空格分隔的长 flag 形密钥
# --------------------------------------------------------------------------- #


class TestSpaceLongFlag:
    def test_space_separated_long_flag_is_masked(self) -> None:
        out = redact_text("mysql --password supersecret123 -h db")
        assert "supersecret123" not in out
        assert "--password" in out  # literal flag stays visible

    def test_quoted_value_with_spaces_masked_as_single_token(self) -> None:
        # `"my secret pw"` is one shell token — masked whole, no mid-word leak.
        out = redact_text('cmd --password "my secret pw" --verbose')
        assert "my secret pw" not in out
        assert "secret" not in out  # the bare-`\\S+` truncation bug must not recur
        assert "--verbose" in out

    def test_long_flag_is_case_insensitive(self) -> None:
        out = redact_text("--Token MyTokenValue999")
        assert "MyTokenValue999" not in out

    def test_value_that_is_another_flag_is_skipped(self) -> None:
        out = redact_text("mysql --password -h dbhost")
        assert "-h dbhost" in out  # `-h` not treated as the password


# --------------------------------------------------------------------------- #
# [B] Known-tool short flags (`mysql -p<v>`, `redis-cli -a <v>`, curl `-u`, …)
#     spec: 需求:`redact_text` 必须脱敏已知客户端工具的短 flag 凭据
# --------------------------------------------------------------------------- #


class TestToolShortFlag:
    def test_mysql_glued_short_flag_masked(self) -> None:
        out = redact_text("mysql -psup3rsecret -h db")
        assert "sup3rsecret" not in out

    def test_escaped_quote_in_value_no_leak(self) -> None:
        # An escaped quote (`\"`) inside a double-quoted value must not falsely
        # close the token and split the secret out.
        out = redact_text(r'mysql -p"super\" secret"')
        assert "secret" not in out

    def test_escaped_quote_in_curl_json_payload_no_cred_leak(self) -> None:
        # A curl JSON payload with escaped quotes (`{\"k\":\"v\"}`) must not
        # break tokenization so the later `-u user:pw` still redacts.
        out = redact_text(r'curl -d "{\"k\":\"v\"}" -u admin:supersecret123 https://h')
        assert "supersecret123" not in out
        assert "admin:" in out  # user preserved

    def test_unterminated_quote_glued_value_masked_not_leaked(self) -> None:
        # An unterminated quote (no closing `"`) grabs to end-of-line so the
        # secret inside it is masked rather than passed through.
        out = redact_text('mysql -p"unterminatedsecret extra')
        assert "unterminatedsecret" not in out

    def test_mysql_spaced_p_is_database_not_masked(self) -> None:
        # `mysql -p <value>` — the value is a database name, must NOT be masked.
        out = redact_text("mysql -p mydatabase")
        assert out == "mysql -p mydatabase"

    def test_mysql_glued_value_equal_to_head_only_masks_suffix(self) -> None:
        # Secret value `mysql` equals the command head; head must survive.
        out = redact_text("mysql -pmysql")
        assert out == "mysql -p****"

    def test_mysql_quoted_value_with_space_no_midword_leak(self) -> None:
        # `-p"my secret"` is a single concatenated token; `secret` must vanish.
        out = redact_text('mysql -p"my secret"')
        assert "secret" not in out
        assert out.startswith("mysql -p")

    def test_redis_cli_spaced_a_flag_masked(self) -> None:
        out = redact_text("redis-cli -a authpw123 ping")
        assert "authpw123" not in out

    def test_redis_cli_repeated_glued_tokens_each_in_place(self) -> None:
        out = redact_text("redis-cli -aget -aget")
        assert out == "redis-cli -a**** -a****"

    def test_sshpass_short_flag_masked(self) -> None:
        out = redact_text("sshpass -p hunter2value ssh user@host")
        assert "hunter2value" not in out

    def test_curl_userinfo_masked_keeps_user(self) -> None:
        out = redact_text("curl -u admin:s3cr3tvalue https://api.host")
        assert "s3cr3tvalue" not in out
        assert "admin" in out  # user preserved

    def test_curl_user_without_colon_not_masked_not_raised(self) -> None:
        out = redact_text("curl -u admin https://api.host")
        assert "admin" in out  # partition(':') guard: no `:` -> untouched

    def test_command_head_punches_through_sudo_docker_exec(self) -> None:
        out = redact_text("sudo docker exec dbc mysql -psup3rsecret")
        assert "sup3rsecret" not in out

    def test_tool_name_in_non_head_position_does_not_fire(self) -> None:
        # head is `echo`, not whitelisted — the `mysql` token is an argument.
        out = redact_text("echo mysql -psecretliteral")
        assert out == "echo mysql -psecretliteral"

    def test_env_prefix_short_flag_still_masked(self) -> None:
        out = redact_text("env FOO=bar mysql -psup3rsecret")
        assert "sup3rsecret" not in out

    def test_unknown_tool_same_shaped_flag_not_masked(self) -> None:
        out = redact_text("myhack -p hunter2value")
        assert out == "myhack -p hunter2value"

    def test_malformed_unterminated_quote_does_not_raise(self) -> None:
        # Must return normally on an unterminated quote (best-effort, no raise).
        out = redact_text('mysql -p"unterminated')
        assert isinstance(out, str)

    def test_no_credential_command_format_preserved(self) -> None:
        # Double space + quotes must survive byte-for-byte (no shlex re-join).
        out = redact_text('redis-cli set mykey "a  b"')
        assert out == 'redis-cli set mykey "a  b"'

    def test_only_credential_token_rewritten_rest_preserved(self) -> None:
        out = redact_text('mysql -psup3rsecret --comment "keep  spaces"')
        assert "sup3rsecret" not in out
        assert '--comment "keep  spaces"' in out  # double space not folded

    def test_mongosh_glued_p_flag_masked(self) -> None:
        out = redact_text("mongosh -psupersecret123")
        assert "supersecret123" not in out

    def test_mongo_glued_p_flag_masked(self) -> None:
        out = redact_text("mongo -psupersecret123")
        assert "supersecret123" not in out

    def test_sshpass_glued_p_flag_masked(self) -> None:
        out = redact_text("sshpass -psupersecret123 ssh user@host")
        assert "supersecret123" not in out

    def test_curl_in_quote_ampersand_does_not_split_segment(self) -> None:
        # The `&` inside the quoted URL must not be treated as a command
        # separator, or the `-u` segment's head is misread and the password
        # leaks past redaction.
        out = redact_text('curl "https://x/?a=1&b=2" -u admin:supersecret123')
        assert "supersecret123" not in out
        assert "admin" in out  # user preserved
        assert "a=1&b=2" in out  # in-quote URL query left intact


class TestWrapperOptions:
    def test_sudo_value_opt_then_mysql_masked(self) -> None:
        # `sudo -n` is a value-less option; the head resolves to `mysql`.
        out = redact_text("sudo -n mysql -psupersecret123")
        assert "supersecret123" not in out

    def test_nice_value_opt_then_mysql_masked(self) -> None:
        # `nice -n 10` consumes the `10` value before `mysql`.
        out = redact_text("nice -n 10 mysql -psupersecret123")
        assert "supersecret123" not in out

    def test_env_dash_i_then_mysql_masked(self) -> None:
        out = redact_text("env -i mysql -psupersecret123")
        assert "supersecret123" not in out

    def test_docker_exec_user_opt_then_mysql_masked(self) -> None:
        out = redact_text("docker exec --user root c mysql -psupersecret123")
        assert "supersecret123" not in out

    def test_sudo_value_taking_opts_consume_value(self) -> None:
        # `-R <dir>` / `-T <timeout>` take a value; the head must still resolve
        # to `mysql`, not the option's value.
        for cmd in ("sudo -R /chroot mysql -psupersecret123", "sudo -T 30 mysql -psupersecret123"):
            assert "supersecret123" not in redact_text(cmd)

    def test_ssh_bind_interface_opt_consumes_value(self) -> None:
        out = redact_text("ssh -B eth0 host mysql -psupersecret123")
        assert "supersecret123" not in out

    def test_time_value_opt_then_mysql_masked(self) -> None:
        out = redact_text("time -p mysql -psupersecret123")
        assert "supersecret123" not in out


# --------------------------------------------------------------------------- #
# [C] URL userinfo (`scheme://user:<pw>@host` / `scheme://<token>@host`)
#     spec: 需求:`redact_text` 必须脱敏 URL userinfo 凭据
# --------------------------------------------------------------------------- #


class TestUrlUserinfo:
    def test_uppercase_scheme_token_masked(self) -> None:
        out = redact_text("HTTPS://ghp_abcd1234efgh5678@github.com/o/r")
        assert "ghp_abcd1234efgh5678" not in out

    def test_two_segment_password_masked_keeps_user(self) -> None:
        out = redact_text("redis://appuser:s3cr3tpw@cache.host:6379")
        assert "s3cr3tpw" not in out
        assert "appuser" in out

    def test_single_segment_token_masked(self) -> None:
        out = redact_text("git clone https://ghp_abcd1234efgh5678@github.com/org/repo")
        assert "ghp_abcd1234efgh5678" not in out

    def test_pure_username_single_segment_over_masked(self) -> None:
        # Accepted security-side over-mask: username is masked rather than
        # risk leaking a `token@host` PAT.
        out = redact_text("ssh://deployuser@host")
        assert "deployuser" not in out

    def test_url_without_userinfo_unchanged(self) -> None:
        out = redact_text("redis://localhost:6379/0")
        assert out == "redis://localhost:6379/0"


# --------------------------------------------------------------------------- #
# [D] Known env-name credentials (`PGPASSWORD=...`, `MYSQL_PWD=...`)
#     spec: 需求:`redact_text` 必须脱敏已知 env 名凭据并排除路径形
# --------------------------------------------------------------------------- #


class TestEnvCredential:
    def test_pgpassword_masked(self) -> None:
        out = redact_text("PGPASSWORD=p@ssw0rdvalue psql -U app")
        assert "p@ssw0rdvalue" not in out

    def test_mysql_pwd_masked(self) -> None:
        out = redact_text("MYSQL_PWD=dbsecretvalue mysql -u root")
        assert "dbsecretvalue" not in out

    def test_pwd_working_directory_not_masked(self) -> None:
        out = redact_text("PWD=/home/alice make build")
        assert "/home/alice" in out

    def test_file_suffix_path_not_masked(self) -> None:
        out = redact_text("MYSQL_PASSWORD_FILE=/run/secrets/db_pass")
        assert "/run/secrets/db_pass" in out

    def test_quoted_value_with_spaces_no_midword_leak(self) -> None:
        out = redact_text('PGPASSWORD="my secret pw" psql -U app')
        assert "my secret pw" not in out
        assert "secret pw" not in out  # the `(\\S+)` truncation leak must not recur

    def test_dirty_glued_quote_value_no_tail_leak(self) -> None:
        # `="sec"rettail` is one shell word (quote concatenated with a bare
        # tail); a plain `"[^"]*"|\\S+` alternation would mask only `"sec"` and
        # leak the glued tail `rettail`.
        out = redact_text('PGPASSWORD="sec"rettail psql')
        assert "rettail" not in out
        assert redact_text(out) == out  # idempotent

    def test_unterminated_quote_value_masked_not_leaked(self) -> None:
        # An unterminated quote after the env name grabs to end-of-line; the
        # secret must be masked, not passed through verbatim.
        out = redact_text('PGPASSWORD="unterminatedlongsecret psql')
        assert "unterminatedlongsecret" not in out
        assert redact_text(out) == out  # idempotent

    def test_escaped_quote_in_env_value_no_tail_leak(self) -> None:
        # A password containing a literal `"` (shell-escaped as `\"`) must not
        # falsely close the quoted span: a non-escape-aware `"[^"]*"` alt would
        # stop at the `\"`, mask only `"a\"`, and leak the cleartext tail
        # (`supersecrettail`) past the next space. The double-quote alt must be
        # escape-aware (mirroring `_SHELL_WORD`).
        out = redact_text(r'PGPASSWORD="a\" supersecrettail" psql')
        assert "supersecrettail" not in out
        assert redact_text(out) == out  # idempotent


# --------------------------------------------------------------------------- #
# Negative precision: non-secret tokens must survive byte-for-byte.
#     spec: 需求:`redact_text` 必须保持负例 precision 与幂等性
# --------------------------------------------------------------------------- #


class TestNegativeProtects:
    def test_ps_p_pid_not_masked(self) -> None:
        assert redact_text("ps -p 1234") == "ps -p 1234"

    def test_kubectl_logs_p_not_masked(self) -> None:
        assert redact_text("kubectl logs -p") == "kubectl logs -p"

    def test_pwd_working_directory_not_masked(self) -> None:
        assert redact_text("PWD=/home/alice make") == "PWD=/home/alice make"

    def test_mysql_password_file_path_not_masked(self) -> None:
        s = "MYSQL_PASSWORD_FILE=/run/secrets/x"
        assert redact_text(s) == s

    def test_unknown_tool_flag_not_masked(self) -> None:
        assert redact_text("myhack -p pw") == "myhack -p pw"

    def test_prose_long_flag_next_word_over_masked(self) -> None:
        # Accepted security-side over-mask (spec L45-47): the token after
        # `--password` in prose is masked to `****`. The literal `--password`
        # stays visible; this is over-mask, not a leak.
        out = redact_text("the --password flag is required")
        assert "****" in out
        assert "--password" in out
        assert "flag" not in out  # the prose word was over-masked

    def test_plain_prose_unchanged(self) -> None:
        s = "restart the mysql service"
        assert redact_text(s) == s


# --------------------------------------------------------------------------- #
# Idempotency: redact_text(redact_text(s)) == redact_text(s) for every rule.
#     spec: 需求:`redact_text` 必须保持负例 precision 与幂等性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line",
    [
        # Existing five classes.
        "password=verylongsecretvalue",
        "secret=topsecretvalue",
        "token=ghp_1234567890abcdefghij",
        "Authorization: Bearer mytokenvalue123456",
        'Authorization: Bearer "a b c"',
        'Authorization: Bearer "my long token value"',
        "eyJ0eXAiOiJKV1Q.eyJleHAiOjE2MDB9.abcDEF1234",
        "sk-abcdefghijklmnopqrstuvwxyz1234567890",
        # [A] long flag — incl. quoted value with spaces (quote-wrapped fixpoint).
        "mysql --password supersecret123 -h db",
        '--password "my secret pw"',
        # [B] tool short flags.
        "mysql -psup3rsecret -h db",
        "mysql -pmysql",
        "redis-cli -aget -aget",
        "sshpass -p hunter2value ssh user@host",
        # [B] mongosh / mongo / sshpass glued `-p`.
        "mongosh -psupersecret123",
        "mongo -psupersecret123",
        "sshpass -psupersecret123",
        # [B] wrapper options before the real command.
        "sudo -n mysql -psupersecret123",
        "nice -n 10 mysql -psupersecret123",
        "env -i mysql -psupersecret123",
        "docker exec --user root c mysql -psupersecret123",
        "time -p mysql -psupersecret123",
        # [F4] in-quote `&` must not split the curl segment.
        'curl "https://x/?a=1&b=2" -u admin:supersecret123',
        # [B] glued quoted value with space — quote-wrapped fixpoint.
        'mysql -p"my secret"',
        # [B] dirty glued quote — true value `sectail` -> `-p****` fixpoint.
        'mysql -p"sec"tail',
        # [B] curl with space-bearing password — partition value has a space,
        #     quote-wrapping (or short `****`) guards the fixpoint.
        'curl -u admin:"se cret"',
        # [C] URL userinfo.
        "redis://appuser:s3cr3tpw@cache.host:6379",
        "git clone https://ghp_abcd1234efgh5678@github.com/org/repo",
        # [D] env credentials.
        "PGPASSWORD=p@ssw0rdvalue psql -U app",
        "MYSQL_PWD=dbsecretvalue mysql -u root",
        # [D] quoted env value with spaces — quote-wrapped fixpoint.
        'PGPASSWORD="my secret pw" psql -U app',
        # [D] dirty glued quote env value — concatenated word -> fixpoint.
        'PGPASSWORD="sec"rettail psql',
        # [D] escaped quote inside env value — escape-aware span, no tail leak.
        r'PGPASSWORD="a\" supersecrettail" psql',
        # [F5] escaped quote inside a value must not break the fixpoint.
        r'mysql -p"super\" secret"',
        r'curl -d "{\"k\":\"v\"}" -u admin:supersecret123 https://h',
        # Unterminated quote (grabbed to EOL) must stay a fixpoint.
        'mysql -p"unterminatedsecret extra',
        'PGPASSWORD="unterminatedlongsecret psql',
        # Already-masked inputs fed back in (second-pass stability).
        "--password ****",
        'mysql -p"my s...cret"',
        "PGPASSWORD=p@ss...alue psql",
    ],
)
def test_idempotent(line: str) -> None:
    once = redact_text(line)
    twice = redact_text(once)
    assert twice == once
