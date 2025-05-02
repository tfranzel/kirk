from kirk.client import Buffer, is_channel_name


def test_buffer_fixed_iteration():
    buf = Buffer[int](5)

    for i in range(3):
        buf.insert(i)
    assert list(buf) == [2, 1, 0]

    # start <= fill
    assert list(buf.fixed_iter(0)) == []
    assert list(buf.fixed_iter(1)) == [0]
    assert list(buf.fixed_iter(2)) == [1, 0]
    assert list(buf.fixed_iter(3)) == [2, 1, 0]
    # overshoot: start > fill
    assert list(buf.fixed_iter(4)) == [2, 1, 0]
    assert list(buf.fixed_iter(5)) == [2, 1, 0]

    # fill completely
    buf.insert(3)
    buf.insert(4)
    assert list(buf) == [4, 3, 2, 1, 0]
    # now full - check proper replacement
    buf.insert(5)
    buf.insert(6)
    assert list(buf) == [6, 5, 4, 3, 2]

    assert buf.idx == 2
    # idx:                    |
    assert buf._buf == [5, 6, 2, 3, 4]
    # all variations
    assert list(buf.fixed_iter(0)) == [4, 3, 2]
    assert list(buf.fixed_iter(1)) == [5, 4, 3, 2]
    assert list(buf.fixed_iter(2)) == [6, 5, 4, 3, 2]
    assert list(buf.fixed_iter(3)) == [2]
    assert list(buf.fixed_iter(4)) == [3, 2]
    assert list(buf.fixed_iter(5)) == [4, 3, 2]
    # overshoot
    assert list(buf.fixed_iter(6)) == [5, 4, 3, 2]
    assert list(buf.fixed_iter(7)) == [6, 5, 4, 3, 2]
    # window shortening
    buf.insert(7)
    assert list(buf.fixed_iter(5)) == [4, 3]


def test_channel_name():
    assert is_channel_name("#foobar")
