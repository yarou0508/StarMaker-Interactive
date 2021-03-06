# -*- coding: utf-8 -*-
"""
Created on Fri Jul 27 14:53:12 2018

@author: 47532
"""

import re
import regex
import tensorflow as tf
import pandas as pd
import numpy as np
import copy
from tensorflow.contrib import rnn
from tqdm import tqdm
import time
import sys

#%% ===================================Clean data ============================= 
def get_data(num):
    file = "./gpu/comment-%s"
    with open(file % num, encoding='utf8', mode='r') as rfile:
        words = []
        sentences = []
        def repl(m):
            inner_word = list(m.group(0))
            return " " + ''.join(inner_word) + " "
        for line in rfile:
            line = line.lower()
            if ('id=' in line) or ('|||' in line) or ('>>>' in line) or ('•' in line) or ('●' in line) or ('╭━╮' in line):
                continue    
            #line = re.sub(r'<.*>', ' ', line)
            line = re.sub('[\=\s+\.\!\?\;\,\/\\\_\。\$\%^*(+\"\:\-\@\#\&\|\[\]\<\>)]+', " ", line)
            line = re.sub('\d{10}', " ", line)
            sentence =  regex.sub(r'\p{So}\p{Sk}*', repl, line)
            word = sentence.split()          
            if len(word) > 1:
                if "'" in word:
                    word.remove("'")
                elif "''" in word:
                    word.remove("''")
                else: 
                    word = word
                words.extend(word)
                sentences.append(word) 
            else:
                continue  
    words_sort = pd.DataFrame(words)[0].value_counts() 
    del words
    words_sort = words_sort[words_sort>1]                        
    word_bank = list(words_sort.index)
    del words_sort
    #word_bank = list(set(words))
    word2id = {}  # convert word => id
    for i in range(len(word_bank)):
        word2id[word_bank[i]] = i+1 
    word2id['!'] = len(word_bank)+1 # add 'EOS' to Word2id
    del word_bank
    inputs = []   
    for sent in sentences: # Loop to process sentence by sentence
        input_sent = []
        for i in range(sent.__len__()):  # Loop to process word by word in one sentence
            input_id = word2id.get(sent[i])
            if not input_id:  
                continue
            input_sent.append(input_id)
        input_sent.append(len(word2id)) # add 'EOS' to the end of a sentence
        if len(input_sent) > 21:
            input_sent = input_sent[:21]
            inputs.append(input_sent)
        elif len(input_sent) < 3:
            continue
        else:
            inputs.append(input_sent)
    del sentences
    pad = len(max(inputs, key=len))
    inputs = [i + [0]*(pad-len(i)) for i in inputs]
    return pad, inputs, len(word2id)+1, word2id

class TrainData:
    def __init__(self, inputs, batch_size, sen_length):
        self.inputs = inputs
        self.batch_size  = batch_size
        self.sen_length = sen_length
        self.n = len(inputs)       
    def get_batch_data(self, batch):
        global batch_size
        start_pos = batch * self.batch_size
        end_pos = min((batch + 1) * self.batch_size, self.n)
        xdata = self.inputs[start_pos:end_pos]
        # rotating the input sentence once to the left to generate the target sentence
        ydata = copy.deepcopy(self.inputs[start_pos:end_pos])
        for row in ydata:
            row.pop(0)
            row.append(0)      
        x_batch = np.array(xdata, dtype=np.int32)
        y_batch = np.array(ydata, dtype=np.int32)
        return x_batch, y_batch
    def get_num_batches(self):
        return max(self.n, 0) // self.batch_size

#%% ===================================LSTM model ============================= 
def build_lstm(hidden_dim, num_layers, batch_size, dropout_rate,sampling):
    # hidden_dim: the number of nodes in one lstm layer
    # num_layers: the number of  lstm layers
    # create a lstm cell
    lstm_cell = tf.nn.rnn_cell.BasicLSTMCell(hidden_dim, forget_bias=1.0, state_is_tuple=True)  
    # add dropout
    if sampling == False:
        lstm_cell = tf.nn.rnn_cell.DropoutWrapper(lstm_cell, output_keep_prob=(1 - dropout_rate))
    # stack several lstm layers 
    lstm_cell= tf.nn.rnn_cell.MultiRNNCell([lstm_cell] * num_layers, state_is_tuple=True)
    initial_state = lstm_cell.zero_state(batch_size, tf.float32)   
    return lstm_cell, initial_state

def build_output(lstm_output, hidden_dim, softmax_w, softmax_b):
    outputs = tf.reshape(tf.concat(lstm_output,1), [-1, hidden_dim])      
    # using softmax to compute probability
    logits = tf.matmul(outputs, softmax_w) + softmax_b    
    preds = tf.nn.softmax(logits, name='predictions')   
    return preds, logits

def build_loss(logits, targets):      
    # Softmax cross entropy loss   
    loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels = tf.reshape(targets, [-1]), logits = logits))   
    return loss

def build_optimizer(loss, learning_rate):
    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate).minimize(loss)   
    return optimizer

class LSTM:    
    def __init__(self, vocab_size, batch_size, 
                 num_batches, num_steps, hidden_dim=64, 
                 num_layers=2, learning_rate=0.001, lambd=0.01, dropout_rate=0.5,  sampling=True):
        #self.graph = tf.Graph()
        self.lr = learning_rate
        self.lambd = lambd
        self.dropout_rate = dropout_rate        
        self.num_layers = num_layers  
        self.num_batches = num_batches
        self.hidden_dim = hidden_dim # BasicLSTM model needs input size = [batch_size, hidden_dim], therefore, embedding_size must = hidden_dim
        self.vocab_size = vocab_size
        if sampling == True:
            self.batch_size, self.num_steps = 1, 1
        else:
            self.batch_size, self.num_steps = batch_size, num_steps
        self.global_step = tf.Variable(0, dtype=tf.int32, trainable=False, name='global_step')    
        self.config = tf.ConfigProto()
        self.config.gpu_options.allow_growth = True
        self.config.allow_soft_placement = True            
        tf.reset_default_graph()  
        #with self.graph.as_default() as g:
        with tf.device('/cpu:0'):
            # initialize the input, target, and test data with placeholder
            self.inputs = tf.placeholder(tf.int32, shape=[None, self.num_steps], name='inputs')
            self.targets = tf.placeholder(tf.int32, shape=[None, self.num_steps], name='targets')
            self.test_word_id = tf.placeholder(tf.int32, shape=[None], name = 'test_word_id')
            # Initialize weights and bias for the softmax node
            with tf.variable_scope('softmax'):
                self.softmax_w = tf.get_variable("softmax_w", shape = [self.hidden_dim, self.vocab_size], 
                                                 regularizer=tf.contrib.layers.l2_regularizer(scale=self.lambd / self.num_batches), 
                                                  initializer = tf.random_uniform_initializer(-1,1,seed=1))
                # tf.truncated_normal_initializer(mean = 0, stddev=1.0 / np.sqrt(self.hidden_dim),seed=1)
                self.softmax_b = tf.get_variable("softmax_b", initializer = tf.zeros([self.vocab_size]))
            self.embedding = tf.get_variable("embedding", shape=[self.vocab_size, self.hidden_dim],
                                     regularizer=tf.contrib.layers.l2_regularizer(scale=self.lambd / self.num_batches),
                                     initializer=tf.random_uniform_initializer(-1,1,seed=1))
            norm = tf.sqrt(tf.reduce_sum(tf.square(self.embedding), 1, keepdims=True))
            self.normed_embedding = self.embedding / norm
            test_embed = tf.nn.embedding_lookup(self.normed_embedding, self.test_word_id)
            self.similarity = tf.matmul(test_embed, tf.transpose(self.normed_embedding), name = 'similarity')
        with tf.device('/gpu:0'):
            # Initialize lstm model
            lstm_cell, self.initial_state = build_lstm(self.hidden_dim, self.num_layers, self.batch_size, self.dropout_rate, sampling)
            # lookup the specific intput in the embedding matrix       
            self.inputs_emb = tf.nn.embedding_lookup(self.embedding, self.inputs)
            self.inputs_emb = tf.unstack(self.inputs_emb, self.num_steps, 1)
            # Run the lstm model
            outputs, self.final_state = rnn.static_rnn(lstm_cell, self.inputs_emb, 
                         initial_state = self.initial_state, dtype=tf.float32)                
            # softmax prediction probability
            self.prediction, self.logits = build_output(outputs, self.hidden_dim, self.softmax_w, self.softmax_b)
            # Loss and optimizer (with gradient clipping)
            self.loss = build_loss(self.logits, self.targets)
            self.optimizer = build_optimizer(self.loss, self.lr)
        
#%% ===================================训练数据 ============================= 
if __name__ == "__main__":
    num = str(sys.argv[1])
    sen_length, inputs, vocab_size, word2id = get_data(num)
    batch_size = 32
    train_data = TrainData(inputs, batch_size, sen_length) #inputs, batch_size, sen_length
    print('Train size: %s' % (train_data.get_num_batches()*batch_size))
    print('Vocab_size: %s' % vocab_size)             
    num_batches = train_data.get_num_batches()
    model = LSTM(vocab_size, batch_size, num_batches, sen_length, sampling=False)  
    epochs = 30
    starttime = time.time()
    with tf.Session(config = model.config) as sess:
        sess.run(tf.group(tf.global_variables_initializer(), tf.local_variables_initializer()))
        writer = tf.summary.FileWriter('./comments_model/LSTM/%s' % num, sess.graph)  # self.global_step.eval(session=sess)
        step = 0
        saver = tf.train.Saver(tf.global_variables(), max_to_keep=1)       
        for index in range(epochs):      
            total_loss = 0.0
            new_state = sess.run([model.initial_state])
            for batch in tqdm(range(num_batches)):
                batch_inputs,batch_targets = train_data.get_batch_data(batch)
                # Feed in the training data
                feed = {model.inputs: batch_inputs, model.targets: batch_targets, model.initial_state: new_state}
                batch_loss, new_state, _ = sess.run([model.loss, model.final_state, model.optimizer], feed_dict=feed)
                total_loss += batch_loss
                step += 1
                if step % 100 == 0:
                    saver.save(sess, './comments_model/LSTM/%s/' % num, global_step=step)
                    if step % 10000 == 0:
                        print(step)
            print('Train Loss at step {}: {:5.6f}'.format(index+1, total_loss / num_batches))
    endtime = time.time()  
    print(endtime - starttime) 
#%% ===================================Generate sentences ============================= 
def pick_top_n(preds, vocab_size, iter, top_n=3):
    # Pick the top n words from predictions using the trained word embedding 
    p = np.squeeze(preds)
    if iter <= 3:
        p = p[1:-1]
    # Re-compute the probabilities
    # Set the non-top n probabilities to 0
        p[np.argsort(p)[:-top_n]] = 0
    # Normalize them
        p = p / np.sum(p)
    # Random select from the top n words based on their nomalized probabilities
        c = np.random.choice(vocab_size-2, 1, p=p)[0]+1
    else:
        p = p[1:]
    # Re-compute the probabilities
    # Set the non-top n probabilities to 0
        p[np.argsort(p)[:-top_n]] = 0
    # Normalize them
        p = p / np.sum(p)
    # Random select from the top n words based on their nomalized probabilities
        c = np.random.choice(vocab_size-1, 1, p=p)[0]+1
    return c

def sample(n_words, vocab_size, batch_size, num_batches, sen_length, prime, num):
    #prime is the start word
    samples=[prime]
    # sampling=True means that batch size=1 x 1
    model = LSTM(vocab_size, batch_size, num_batches, sen_length, sampling=True)
    saver = tf.train.Saver()
    with tf.Session(config = model.config) as sess:
        # Load precious model parameters
        checkpoint_file = tf.train.latest_checkpoint('./comments_model/LSTM/%s' % num)
        saver.restore(sess, checkpoint_file)
        new_state = sess.run([model.initial_state])
        # generate words one by one to form a sentence until it get the 'EOS'
        c = word2id.get(prime)
        for i in range(n_words):
            test_word_id = c
            if test_word_id == word2id.get('EOS'):
                break
            else:
                feed = {model.inputs: [[test_word_id]],
                        model.initial_state: new_state}
                preds, new_state = sess.run([model.prediction, model.final_state], feed_dict=feed)
                c = pick_top_n(preds, vocab_size, i)
                while c == test_word_id:
                    c = pick_top_n(preds, vocab_size, i)
                samples.extend(x for x,v in word2id.items() if v==c)
    print(' '.join(samples))

if __name__ == "__main__":
    num = str(sys.argv[1])
    sen_length, inputs, vocab_size, word2id = get_data(num)
    batch_size = 128
    train_data = TrainData(inputs, batch_size, sen_length) #inputs, batch_size, sen_length
    print('Train size: %s' % (train_data.get_num_batches()*batch_size))
    print('Vocab_size: %s' % vocab_size)
    num_batches = train_data.get_num_batches()
    result = pd.DataFrame(columns = ['sentence','prob'])
    first_words = []
    for i in range(len(inputs)):
        first_words.append(inputs[i][0])
    for j in first_words:
        (sentence, probability) = sample(20, vocab_size, batch_size, num_batches, sen_length, j, num)
        result = result.append({'sentence':sentence, 'prob': probability}, ignore_index = True)
    result_sort = result.sort_values(['prob'],ascending=False)
    result_sort.to_csv('./gpu/output_sentences.csv')
    for i in range(4):
        #sample(10, vocab_size, batch_size, num_batches, prime = "you")
        for j in ['nice', 'perfect','superb', 'very', 'wow', 'you', 'i', 'bro', 'sis', 'looks', 'sounds', 'both', 'this']:
            sample(20, vocab_size, batch_size, num_batches, sen_length, j, num)

#%% =================================== Evaluate trained word embedding============================= 
def predict(test_words, sen_length):
    model = LSTM(vocab_size, batch_size, num_batches, sen_length, sampling=True)
    saver = tf.train.Saver()
    with tf.Session(config = model.config) as sess:
        checkpoint_file = tf.train.latest_checkpoint('./comments_model/LSTM/%s' % num)
        saver.restore(sess, checkpoint_file)   
        test_word_id = [word2id.get(x) for x in test_words]
        feed_dict = {model.test_word_id: test_word_id}
        similarity = sess.run(model.similarity, feed_dict=feed_dict)
        top_k = 8
        for i in range(len(test_words)):
            nearest = (-similarity[i, :]).argsort()[1:top_k+1]        
            log = "Nearest to '%s':" % test_words[i]
            for k in range(top_k):
                close_word = [x for x,v in word2id.items() if v == nearest[k]]
                log = '%s %s,' % (log, close_word)
            print(log)
test_words = ['nice', 'perfect' ,'song', 'superb', '😘', 'voice', 'i','bro', 'sis', 'looks', 'sounds', 'both']
if __name__ == "__main__":
    num = str(sys.argv[1])
    sen_length, inputs, vocab_size, word2id = get_data(num)
    batch_size = 32
    train_data = TrainData(inputs, batch_size, sen_length) #inputs, batch_size, sen_length
    print('Train size: %s' % (train_data.get_num_batches()*batch_size))
    print('Vocab_size: %s' % vocab_size)            
    num_batches = train_data.get_num_batches()
    predict(test_words, sen_length)


#%% ===================================using Word2Vec package ============================= 
def get_data():
    with open('comment1', encoding='utf8', mode='r') as rfile:
        words = []
        sentences = []
        def repl(m):
            inner_word = list(m.group(0))
            return " " + ''.join(inner_word) + " "
        for line in rfile:
            line = line.lower()
            line = re.sub(r'<.*>', ' ', line)
            line = re.sub('[\s+\.\!\?\,\/_,$%^*(+\"\:\-\@\#\&)]+', " ", line)
            sentence =  regex.sub(r'\p{So}\p{Sk}*', repl, line)
            word = sentence.split()                        
            if len(word) > 1:
                if "'" in word:
                    word.remove("'")
                else: 
                    word = word
                words.extend(word)
                sentences.append(word) 
                sentences.append('EOS')
            else:
                continue  
    return sentences

from gensim.models.word2vec import Word2Vec 
model = Word2Vec(get_data(), size=32, window=5, min_count=1)
model.save('./comments_model/Word2Vec')
word_vectors = model.wv
for i in test_words:
   y = model.most_similar('nice')
   
for i in y:
    print(i[0])