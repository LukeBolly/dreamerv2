import tensorflow as tf


# implementation based off https://www.tensorflow.org/tutorials/text/transformer
class MultiHeadAttention(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads):
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads
        self.d_model = d_model

        assert d_model % self.num_heads == 0

        self.depth = d_model // self.num_heads

        self.wq = tf.keras.layers.Dense(d_model, kernel_initializer='glorot_uniform', use_bias=False)
        self.wk = tf.keras.layers.Dense(d_model, kernel_initializer='glorot_uniform', use_bias=False)
        self.wv = tf.keras.layers.Dense(d_model, kernel_initializer='glorot_uniform', use_bias=False)

        self.ff = tf.keras.layers.Dense(d_model, kernel_initializer='glorot_uniform', use_bias=False)

    def split_heads(self, x, batch_size):
        """Split the last dimension into (num_heads, depth).
        Transpose the result such that the shape is (batch_size, num_heads, seq_len, depth)
        """
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, q, k, v, mask):
        batch_size = tf.shape(q)[0]

        q = self.wq(q)  # (batch_size, seq_len, d_model)
        k = self.wk(k)  # (batch_size, seq_len, d_model)
        v = self.wv(v)  # (batch_size, seq_len, d_model)

        q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
        k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
        v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)

        # scaled_attention.shape == (batch_size, num_heads, seq_len_q, depth)
        # attention_weights.shape == (batch_size, num_heads, seq_len_q, seq_len_k)
        scaled_attention, attention_weights = scaled_dot_product_attention(q, k, v, mask)

        scaled_attention = tf.transpose(scaled_attention, perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)

        concat_attention = tf.reshape(scaled_attention, (batch_size, -1, self.d_model))  # (batch_size, seq_len_q, d_model)

        output = self.ff(concat_attention)  # (batch_size, seq_len_q, d_model)

        return output, attention_weights


class PointwiseFeedforward(tf.keras.layers.Layer):
    def __init__(self, d_model):
        super(PointwiseFeedforward, self).__init__()
        self.ff1 = tf.keras.layers.Dense(d_model, kernel_initializer='glorot_uniform')
        self. ff2 = tf.keras.layers.Dense(d_model, kernel_initializer='glorot_uniform')

    def call(self, x):
        x = self.ff1(x)
        x = tf.nn.leaky_relu(x)
        x = self.ff2(x)
        return x


class TransformerLayer(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads):
        super(TransformerLayer, self).__init__()

        self.mha = MultiHeadAttention(d_model, num_heads)
        self.ffn = PointwiseFeedforward(d_model)

        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

    def call(self, x, y, mask):
        attn_output, _ = self.mha(x, y, y, mask)  # (batch_size, input_seq_len, d_model)
        out1 = self.layernorm1(x + attn_output)  # (batch_size, input_seq_len, d_model)

        ffn_output = self.ffn(out1)  # (batch_size, input_seq_len, d_model)
        out2 = self.layernorm2(out1 + ffn_output)  # (batch_size, input_seq_len, d_model)

        return out2


class PoolingMultiheadAttention(tf.keras.layers.Layer):
    def __init__(self, d_model, num_seed_vectors, num_heads):
        super().__init__()
        self.mab = TransformerLayer(d_model, num_heads)
        self.seed_vectors = tf.Variable(tf.random.normal([1, num_seed_vectors, d_model]))

    def call(self, x, mask):
        b = tf.shape(x)[0]
        s = self.seed_vectors
        s = tf.tile(s, [b, 1, 1])  # shape [b, k, d]

        return self.mab(s, x, mask)


def scaled_dot_product_attention(q, k, v, mask):
    """Calculate the attention weights.
    q, k, v must have matching leading dimensions.
    k, v must have matching penultimate dimension, i.e.: seq_len_k = seq_len_v.
    The mask has different shapes depending on its type(padding or look ahead)
    but it must be broadcastable for addition.
    Args:
      q: query shape == (..., seq_len_q, depth)
      k: key shape == (..., seq_len_k, depth)
      v: value shape == (..., seq_len_v, depth_v)
      mask: Float tensor with shape broadcastable
            to (..., seq_len_q, seq_len_k). Defaults to None.
    Returns:
      output, attention_weights
    """

    matmul_qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)

    # scale matmul_qk
    dk = tf.cast(tf.shape(k)[-1], tf.float32)
    matmul_qk = tf.cast(matmul_qk, tf.float32)   # use float32 for attention, not enough precision otherwise for masking operation to properly zero-out masked values
    scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)

    if mask is not None:
        scaled_attention_logits += (mask * -1e9)

    # softmax is normalized on the last axis (seq_len_k) so that the scores
    # add up to 1.
    attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1) # (..., seq_len_q, seq_len_k)

    output = tf.cast(tf.matmul(attention_weights, tf.cast(v, tf.float32)), v.dtype)  # (..., seq_len_q, depth_v)

    return output, attention_weights