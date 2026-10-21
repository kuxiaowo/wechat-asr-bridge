"""Locate native encoder entry points without pinning an application version."""
import struct


WETYPE_ENCODER_SIGNATURES = {
    0xb2c090: bytes.fromhex('33c0c74110803e00004889014889410866894124884126488bc1c74114010000'),
    0xb2c350: bytes.fromhex('48895c241848896c2420565741574883ec40488bf94533ff488b09418bd9418b'),
    0xb2c110: bytes.fromhex('4053574155415641574883ec304c8bac248800000033db4d8bf94c8bf2488bf9'),
    0xb2c0d0: bytes.fromhex('40534883ec20488bd9488b094885c9740ce8fa4b000048c70300000000488b4b'),
}


def resolve_wetype_encoder(data):
    """Require one unambiguous executable-code match for each encoder function."""
    try:
        if data[:2] != b'MZ':
            raise ValueError('Invalid PE executable')
        pe = struct.unpack_from('<I', data, 0x3c)[0]
        if data[pe:pe + 4] != b'PE\0\0' or struct.unpack_from('<H', data, pe + 4)[0] != 0x8664:
            raise ValueError('Expected an x64 PE executable')
        count = struct.unpack_from('<H', data, pe + 6)[0]
        start = pe + 24 + struct.unpack_from('<H', data, pe + 20)[0]
        regions = []
        for offset in range(start, start + count * 40, 40):
            _, rva, size, raw = struct.unpack_from('<IIII', data, offset + 8)
            flags = struct.unpack_from('<I', data, offset + 36)[0]
            if flags & 0x20000000:
                if raw + size > len(data):
                    raise ValueError('Truncated executable section')
                regions.append((rva, data[raw:raw + size]))
        result = {}
        for original, signature in WETYPE_ENCODER_SIGNATURES.items():
            matches = []
            for rva, section in regions:
                position = section.find(signature)
                while position >= 0:
                    matches.append(rva + position)
                    position = section.find(signature, position + 1)
            if len(matches) != 1:
                raise ValueError('Encoder function signature missing or ambiguous: ' + hex(original))
            result[original] = matches[0]
        return result
    except (struct.error, IndexError) as error:
        raise ValueError('Invalid PE executable') from error
