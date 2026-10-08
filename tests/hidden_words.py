"""Words the public files must not contain, kept as (length, sha256) so this file does not spell them out.
found(text) returns the hashes of the ones that occur in text."""
import hashlib

HIDDEN = (
    (2, "867d0e70073530dd4b637b2234b41e6de069c2375decbf9b7a823623b039844c"),
    (3, "444d13e1911ba9aa9b8109b9dab33a26aaa988e956bde8eb7e574f8f5fa5e73f"),
    (4, "0631b7ffb2ea9e6b4b1e139334157b47df9728ec055f9b5e23987ef902a1edef"),
    (5, "2eb176eceda31956f5918bd87f52c96cb22d02f6639a076ae53cf7a1ab3d5714"),
    (5, "7907bf218fa788eeb6391cdd830f37989fbc9f4ab65783fbed0828c2eea170f5"),
    (5, "812a44edda7cb53b14bdf7fcb2415ae67ba7b1c6eff8404db8c742370200a3c2"),
    (5, "821cade0e28dc3a62e567dbf4de93e1e695ada71fc4085b564feaa2c5b661421"),
    (6, "c8b348ba40b691c92dfd53610ccee21a8bbba855e83371b74e38253a8e0e61ee"),
    (7, "81f2748a4e05c27a732a9229f367617eddbf8c4375eee11cc7ecf69d097f086b"),
    (8, "14b49e5bd19a60e5ba66225cdd649db83a60e32e95caa953655687ba03856d7e"),
    (8, "c1e395fcc437d552a2fe7382672a47c7b79e56f1391f6c82b1000371b47a4620"),
    (9, "0dc57c3e5e3ade4b7d72dafa3e65616cb004efa0e492cf348eaa2e90ba5452b0"),
    (10, "132c6ded43ea5e4557c1efe30bbf81274afb4b6b32c376060bd44ecc7f68ca16"),
    (10, "1a537a2160012775fe725b5928d2fc81b82c28834eadb49d7c179d79f52f8ce1"),
    (10, "772f8ed606175ed52609d3cb27f8f3696f733e1607b8b19b610594b311f92079"),
    (11, "761de9c72f4032fa7eed2290b4873cf8bf019e7fdbfe0db4548e028cc22a411b"),
    (11, "85b045ea2a5c9fe8c0b1c097f624d88693580861c35a0dd541f0779a3e73ab38"),
    (11, "df024a8b4fe5657b36f1ab4c24ed6ca0a45c5de9abf45718b5f64caef6383b49"),
)


def found(text):
    hits = []
    for n, digest in HIDDEN:
        seen = {text[i:i + n] for i in range(len(text) - n + 1)}
        if any(hashlib.sha256(s.encode("utf-8")).hexdigest() == digest for s in seen):
            hits.append(digest[:12])
    return hits
