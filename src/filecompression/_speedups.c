/*
 * C versions of the codec hot loops (Huffman, LZW, RLE).
 *
 * Every function produces exactly the same bytes as the pure Python code in
 * filecompression/algorithms, which validates metadata and calls in here only for
 * the per-byte work. Corrupt input raises ValueError; the Python side turns it into
 * CorruptDataError. The GIL is released while a loop runs, so a single-worker
 * server stays responsive during compression.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

#define LZW_MAX_CODE 65535u
#define LZW_HASH_BITS 18
#define LZW_HASH_SIZE (1u << LZW_HASH_BITS)
#define RLE_RECORD 5
#define RLE_MAX_RUN 0xFFFFFFFFull
/* The bit accumulator holds up to 7 pending bits plus one code. */
#define HUFFMAN_MAX_CODE_LENGTH 57

/* ------------------------------------------------------------ histogram */

static PyObject *
histogram(PyObject *module, PyObject *args)
{
    Py_buffer data;
    uint64_t counts[256] = {0};
    if (!PyArg_ParseTuple(args, "y*", &data)) {
        return NULL;
    }
    const unsigned char *input = data.buf;
    Py_ssize_t length = data.len;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t index = 0; index < length; index++) {
        counts[input[index]]++;
    }
    Py_END_ALLOW_THREADS
    PyBuffer_Release(&data);

    PyObject *result = PyList_New(256);
    if (result == NULL) {
        return NULL;
    }
    for (int symbol = 0; symbol < 256; symbol++) {
        PyObject *value = PyLong_FromUnsignedLongLong(counts[symbol]);
        if (value == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyList_SET_ITEM(result, symbol, value);
    }
    return result;
}

/* ------------------------------------------------------------ Huffman */

/* huffman_encode(data, codes, lengths) -> (payload, bit_count)
 * codes: 256 native uint64 values (array "Q"), lengths: 256 bytes; MSB-first bit order. */
static PyObject *
huffman_encode(PyObject *module, PyObject *args)
{
    Py_buffer data, codes, lengths;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "y*y*y*", &data, &codes, &lengths)) {
        return NULL;
    }
    if (codes.len != 256 * (Py_ssize_t)sizeof(uint64_t) || lengths.len != 256) {
        PyErr_SetString(PyExc_ValueError, "Huffman code table must have 256 entries");
        goto done;
    }
    uint64_t code_table[256];
    unsigned char length_table[256];
    memcpy(code_table, codes.buf, sizeof code_table);
    memcpy(length_table, lengths.buf, sizeof length_table);
    for (int symbol = 0; symbol < 256; symbol++) {
        if (length_table[symbol] > HUFFMAN_MAX_CODE_LENGTH) {
            PyErr_SetString(PyExc_ValueError, "Huffman code is too long");
            goto done;
        }
    }

    const unsigned char *input = data.buf;
    Py_ssize_t size = data.len;
    uint64_t total_bits = 0;
    for (Py_ssize_t index = 0; index < size; index++) {
        unsigned char bits = length_table[input[index]];
        if (bits == 0) {
            PyErr_SetString(PyExc_ValueError, "Symbol has no Huffman code");
            goto done;
        }
        total_bits += bits;
    }
    if ((total_bits + 7) / 8 > PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_OverflowError, "Huffman output is too large");
        goto done;
    }

    PyObject *payload = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)((total_bits + 7) / 8));
    if (payload == NULL) {
        goto done;
    }
    unsigned char *output = (unsigned char *)PyBytes_AS_STRING(payload);
    Py_BEGIN_ALLOW_THREADS
    uint64_t buffer = 0;
    int filled = 0;
    size_t position = 0;
    for (Py_ssize_t index = 0; index < size; index++) {
        unsigned char symbol = input[index];
        buffer = (buffer << length_table[symbol]) | code_table[symbol];
        filled += length_table[symbol];
        while (filled >= 8) {
            filled -= 8;
            output[position++] = (unsigned char)(buffer >> filled);
        }
    }
    if (filled > 0) {
        output[position++] = (unsigned char)(buffer << (8 - filled));
    }
    Py_END_ALLOW_THREADS
    result = Py_BuildValue("(NK)", payload, (unsigned long long)total_bits);

done:
    PyBuffer_Release(&data);
    PyBuffer_Release(&codes);
    PyBuffer_Release(&lengths);
    return result;
}

/* huffman_decode(payload, bit_count, children, expected_size) -> bytes
 * children: native int array, two entries (left, right) per internal node, node 0 is
 * the root; a child >= 0 is an internal node, a negative child is the leaf ~symbol. */
static PyObject *
huffman_decode(PyObject *module, PyObject *args)
{
    Py_buffer payload, tree;
    unsigned long long bit_count, expected;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "y*Ky*K", &payload, &bit_count, &tree, &expected)) {
        return NULL;
    }
    Py_ssize_t node_count = tree.len / (Py_ssize_t)(2 * sizeof(int));
    if (node_count == 0 || tree.len % (Py_ssize_t)(2 * sizeof(int)) || bit_count > (unsigned long long)payload.len * 8 || expected > PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_ValueError, "Invalid Huffman tree or bit count");
        goto done;
    }
    const int *children = tree.buf;
    for (Py_ssize_t index = 0; index < node_count * 2; index++) {
        if (children[index] >= node_count || children[index] < -256) {
            PyErr_SetString(PyExc_ValueError, "Invalid Huffman tree");
            goto done;
        }
    }

    PyObject *restored = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)expected);
    if (restored == NULL) {
        goto done;
    }
    unsigned char *output = (unsigned char *)PyBytes_AS_STRING(restored);
    const unsigned char *input = payload.buf;
    const char *error = NULL;
    uint64_t produced = 0;
    int state = 0;
    Py_BEGIN_ALLOW_THREADS
    uint64_t full_bytes = bit_count / 8;
    int tail_bits = (int)(bit_count % 8);
    for (uint64_t index = 0; index <= full_bytes && error == NULL; index++) {
        int bits = 8;
        if (index == full_bytes) {
            if (tail_bits == 0) {
                break;
            }
            if (input[index] & ((1 << (8 - tail_bits)) - 1)) {
                error = "Invalid Huffman padding";
                break;
            }
            bits = tail_bits;
        }
        unsigned value = input[index];
        for (int shift = 7; shift > 7 - bits; shift--) {
            int next = children[2 * state + ((value >> shift) & 1)];
            if (next < 0) {
                if (produced == expected) {
                    error = "Huffman stream contains extra data";
                    break;
                }
                output[produced++] = (unsigned char)~next;
                state = 0;
            } else {
                state = next;
            }
        }
    }
    if (error == NULL && (produced != expected || state != 0)) {
        error = "Truncated Huffman bit stream";
    }
    Py_END_ALLOW_THREADS
    if (error != NULL) {
        Py_DECREF(restored);
        PyErr_SetString(PyExc_ValueError, error);
        goto done;
    }
    result = restored;

done:
    PyBuffer_Release(&payload);
    PyBuffer_Release(&tree);
    return result;
}

/* ------------------------------------------------------------ LZW */

/* lzw_compress(data) -> bytes of big-endian 16-bit codes; data must not be empty. */
static PyObject *
lzw_compress(PyObject *module, PyObject *args)
{
    Py_buffer data;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "y*", &data)) {
        return NULL;
    }
    if (data.len == 0) {
        result = PyBytes_FromStringAndSize(NULL, 0);
        goto done;
    }
    /* Open addressing on (phrase code << 8 | byte); a key is stored plus one so 0 means empty. */
    uint32_t *keys = PyMem_RawCalloc(LZW_HASH_SIZE, sizeof(uint32_t));
    uint16_t *values = PyMem_RawMalloc(LZW_HASH_SIZE * sizeof(uint16_t));
    unsigned char *output = PyMem_RawMalloc((size_t)data.len * 2);
    if (keys == NULL || values == NULL || output == NULL) {
        PyErr_NoMemory();
        goto release;
    }
    const unsigned char *input = data.buf;
    size_t written = 0;
    Py_BEGIN_ALLOW_THREADS
    uint32_t phrase = input[0];
    uint32_t next_code = 256;
    for (Py_ssize_t index = 1; index < data.len; index++) {
        uint32_t key = (phrase << 8 | input[index]) + 1;
        uint32_t slot = (key * 2654435761u) >> (32 - LZW_HASH_BITS);
        while (keys[slot] != 0 && keys[slot] != key) {
            slot = (slot + 1) & (LZW_HASH_SIZE - 1);
        }
        if (keys[slot] == key) {
            phrase = values[slot];
            continue;
        }
        output[written++] = (unsigned char)(phrase >> 8);
        output[written++] = (unsigned char)phrase;
        if (next_code <= LZW_MAX_CODE) {
            keys[slot] = key;
            values[slot] = (uint16_t)next_code++;
        }
        phrase = input[index];
    }
    output[written++] = (unsigned char)(phrase >> 8);
    output[written++] = (unsigned char)phrase;
    Py_END_ALLOW_THREADS
    result = PyBytes_FromStringAndSize((const char *)output, (Py_ssize_t)written);

release:
    PyMem_RawFree(keys);
    PyMem_RawFree(values);
    PyMem_RawFree(output);
done:
    PyBuffer_Release(&data);
    return result;
}

/* lzw_decompress(payload, limit) -> bytes; payload holds big-endian 16-bit codes. */
static PyObject *
lzw_decompress(PyObject *module, PyObject *args)
{
    Py_buffer payload;
    unsigned long long limit;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "y*K", &payload, &limit)) {
        return NULL;
    }
    if (payload.len % 2) {
        PyErr_SetString(PyExc_ValueError, "Invalid LZW metadata or payload length");
        goto done;
    }
    Py_ssize_t count = payload.len / 2;
    if (count == 0) {
        result = PyBytes_FromStringAndSize(NULL, 0);
        goto done;
    }
    if (limit > PY_SSIZE_T_MAX) {
        limit = PY_SSIZE_T_MAX;
    }
    /* Entry = prefix entry + suffix byte; entries are rebuilt by walking the prefix chain. */
    uint16_t *prefix = PyMem_RawMalloc((LZW_MAX_CODE + 1) * sizeof(uint16_t));
    unsigned char *suffix = PyMem_RawMalloc(LZW_MAX_CODE + 1);
    unsigned char *first = PyMem_RawMalloc(LZW_MAX_CODE + 1);
    uint32_t *length = PyMem_RawMalloc((LZW_MAX_CODE + 1) * sizeof(uint32_t));
    size_t capacity = (size_t)count * 4 < limit ? (size_t)count * 4 : (size_t)limit;
    if (capacity < 1) {
        capacity = 1;
    }
    unsigned char *output = PyMem_RawMalloc(capacity);
    if (prefix == NULL || suffix == NULL || first == NULL || length == NULL || output == NULL) {
        PyErr_NoMemory();
        goto release;
    }
    const unsigned char *input = payload.buf;
    const char *error = NULL;
    int out_of_memory = 0;
    size_t written = 0;
    Py_BEGIN_ALLOW_THREADS
    for (uint32_t code = 0; code < 256; code++) {
        suffix[code] = first[code] = (unsigned char)code;
        prefix[code] = 0;
        length[code] = 1;
    }
    uint32_t dictionary_size = 256;
    uint32_t previous = (uint32_t)input[0] << 8 | input[1];
    if (previous > 255) {
        error = "Invalid first LZW code";
    } else {
        output[written++] = (unsigned char)previous;
    }
    for (Py_ssize_t index = 1; index < count && error == NULL; index++) {
        uint32_t code = (uint32_t)input[2 * index] << 8 | input[2 * index + 1];
        uint32_t entry_length;
        unsigned char entry_first;
        if (code < dictionary_size) {
            entry_length = length[code];
            entry_first = first[code];
        } else if (code == dictionary_size) {
            entry_length = length[previous] + 1;
            entry_first = first[previous];
        } else {
            error = "Invalid LZW code";
            break;
        }
        if (written + entry_length > limit) {
            error = "LZW output exceeds the safety limit";
            break;
        }
        if (written + entry_length > capacity) {
            size_t grown = capacity;
            while (grown < written + entry_length) {
                grown *= 2;
            }
            if (grown > limit) {
                grown = (size_t)limit;
            }
            unsigned char *larger = PyMem_RawRealloc(output, grown);
            if (larger == NULL) {
                out_of_memory = 1;
                break;
            }
            output = larger;
            capacity = grown;
        }
        /* "code == dictionary_size" is previous + its own first byte. */
        uint32_t walk = code < dictionary_size ? code : previous;
        size_t end = written + length[walk];
        if (code == dictionary_size) {
            output[end] = entry_first;
        }
        for (size_t position = end; position > written; position--) {
            output[position - 1] = suffix[walk];
            walk = prefix[walk];
        }
        written += entry_length;
        if (dictionary_size <= LZW_MAX_CODE) {
            prefix[dictionary_size] = (uint16_t)previous;
            suffix[dictionary_size] = entry_first;
            first[dictionary_size] = first[previous];
            length[dictionary_size] = length[previous] + 1;
            dictionary_size++;
        }
        previous = code;
    }
    Py_END_ALLOW_THREADS
    if (out_of_memory) {
        PyErr_NoMemory();
    } else if (error != NULL) {
        PyErr_SetString(PyExc_ValueError, error);
    } else {
        result = PyBytes_FromStringAndSize((const char *)output, (Py_ssize_t)written);
    }

release:
    PyMem_RawFree(prefix);
    PyMem_RawFree(suffix);
    PyMem_RawFree(first);
    PyMem_RawFree(length);
    PyMem_RawFree(output);
done:
    PyBuffer_Release(&payload);
    return result;
}

/* ------------------------------------------------------------ RLE */

static size_t
rle_records(uint64_t run)
{
    return (size_t)((run + RLE_MAX_RUN - 1) / RLE_MAX_RUN);
}

static unsigned char *
rle_write(unsigned char *output, uint64_t run, unsigned char value)
{
    while (run > 0) {
        uint64_t piece = run > RLE_MAX_RUN ? RLE_MAX_RUN : run;
        output[0] = (unsigned char)(piece >> 24);
        output[1] = (unsigned char)(piece >> 16);
        output[2] = (unsigned char)(piece >> 8);
        output[3] = (unsigned char)piece;
        output[4] = value;
        output += RLE_RECORD;
        run -= piece;
    }
    return output;
}

/* rle_compress(data) -> bytes of (>I count, byte) records. */
static PyObject *
rle_compress(PyObject *module, PyObject *args)
{
    Py_buffer data;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "y*", &data)) {
        return NULL;
    }
    const unsigned char *input = data.buf;
    Py_ssize_t size = data.len;
    size_t records = 0;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t start = 0, end; start < size; start = end) {
        for (end = start + 1; end < size && input[end] == input[start]; end++) {
        }
        records += rle_records((uint64_t)(end - start));
    }
    Py_END_ALLOW_THREADS
    result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)(records * RLE_RECORD));
    if (result != NULL) {
        unsigned char *output = (unsigned char *)PyBytes_AS_STRING(result);
        Py_BEGIN_ALLOW_THREADS
        for (Py_ssize_t start = 0, end; start < size; start = end) {
            for (end = start + 1; end < size && input[end] == input[start]; end++) {
            }
            output = rle_write(output, (uint64_t)(end - start), input[start]);
        }
        Py_END_ALLOW_THREADS
    }
    PyBuffer_Release(&data);
    return result;
}

/* rle_decompress(payload, limit) -> bytes */
static PyObject *
rle_decompress(PyObject *module, PyObject *args)
{
    Py_buffer payload;
    unsigned long long limit;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "y*K", &payload, &limit)) {
        return NULL;
    }
    if (payload.len % RLE_RECORD) {
        PyErr_SetString(PyExc_ValueError, "Invalid RLE stream");
        goto done;
    }
    const unsigned char *input = payload.buf;
    Py_ssize_t records = payload.len / RLE_RECORD;
    uint64_t total = 0;
    for (Py_ssize_t index = 0; index < records; index++) {
        const unsigned char *record = input + index * RLE_RECORD;
        uint32_t run = (uint32_t)record[0] << 24 | (uint32_t)record[1] << 16 | (uint32_t)record[2] << 8 | record[3];
        if (run == 0) {
            PyErr_SetString(PyExc_ValueError, "RLE run cannot be empty");
            goto done;
        }
        total += run;
    }
    if (total > limit || total > PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_ValueError, "RLE output exceeds the safety limit");
        goto done;
    }
    result = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)total);
    if (result != NULL) {
        unsigned char *output = (unsigned char *)PyBytes_AS_STRING(result);
        Py_BEGIN_ALLOW_THREADS
        for (Py_ssize_t index = 0; index < records; index++) {
            const unsigned char *record = input + index * RLE_RECORD;
            uint32_t run = (uint32_t)record[0] << 24 | (uint32_t)record[1] << 16 | (uint32_t)record[2] << 8 | record[3];
            memset(output, record[4], run);
            output += run;
        }
        Py_END_ALLOW_THREADS
    }

done:
    PyBuffer_Release(&payload);
    return result;
}

/* ------------------------------------------------------------ module */

static PyMethodDef methods[] = {
    {"histogram", histogram, METH_VARARGS, "Count every byte value; returns a list of 256 ints."},
    {"huffman_encode", huffman_encode, METH_VARARGS, "Pack data with a Huffman code table."},
    {"huffman_decode", huffman_decode, METH_VARARGS, "Decode a Huffman bit stream with a flattened tree."},
    {"lzw_compress", lzw_compress, METH_VARARGS, "LZW-compress data into big-endian 16-bit codes."},
    {"lzw_decompress", lzw_decompress, METH_VARARGS, "Restore LZW codes, refusing more than limit bytes."},
    {"rle_compress", rle_compress, METH_VARARGS, "Encode data as (count, byte) RLE records."},
    {"rle_decompress", rle_decompress, METH_VARARGS, "Restore RLE records, refusing more than limit bytes."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT, "_speedups", "C hot loops for the FileComp codecs.", -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit__speedups(void)
{
    return PyModule_Create(&module_definition);
}
