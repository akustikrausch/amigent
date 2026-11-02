from amigent import amigactl, channel, client, errors


def test_one_error_type_under_every_old_address():
    assert amigactl.AmigaError is errors.AmigaError is channel.AmigaError
    assert amigactl.ExecResult is client.ExecResult and amigactl.join_path is client.join_path
    assert errors.AmigaError("no", 5).code == 5


def test_both_clients_flush_alike():
    assert amigactl.AmigaCtl.flush is channel.AmigaChannel.flush
    ran = []

    class Fake(client.VolumeFlush):
        def exec(self, line):
            ran.append(line)

    Fake().flush(["SYS:", "Work:"])
    assert ran == ["Lock SYS: ON", "Lock SYS: OFF", "Lock Work: ON", "Lock Work: OFF"]


def test_commands_tells_link_and_tls_without_peeking():
    ctl = channel.Commands("127.0.0.1")
    assert not ctl.connected and not ctl.is_tls
