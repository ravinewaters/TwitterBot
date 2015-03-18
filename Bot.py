__author__ = 'redhat'

# You need parameters.txt (optional) and status.txt for this bot to work.
# parameters.txt: contains keywords to track to using streaming API.
# status.txt: contains statuses of which the auto_reply use to reply
# You need TwitterAPI and its dependencies. Do `pip install twitterapi`.
# TwitterBot version 0.2

import argparse
import signal
import sys
import os
import configparser
import threading
from time import time, sleep
from collections import deque
from random import uniform, choice
from multiprocessing import Process, Pipe

from TwitterAPI import TwitterAPI


class AutoReplier(TwitterAPI):
    def __init__(self, auth, proxies, status_file):
        self.auth = auth
        self.proxies = proxies
        self.status_file = status_file
        self.mentioned = set()

    def start(self, tweet_q):
        auto_reply = self.auto_reply
        self.load_mentioned('mentioned.txt')

        sleep(15)
        next_call = time()
        while True:
            try:
                screen_name, status_id = tweet_q.pop()
                auto_reply(screen_name, status_id)
            except IndexError:
                pass
            else:
                auto_reply(screen_name, status_id)
            next_call = next_call + uniform(35, 45)
            sleep(next_call - time())

    def save_mentioned(self):
        with open('mentioned.txt', 'w') as f:
            print('Saving mentioned.txt.')
            for item in self.mentioned:
                f.write(item + '\n')

    def load_mentioned(self, mentioned_file):
        # only use mentioned_file if it is modified within two days
        twodays_ago = time() - 60*60*24*2
        if os.path.isfile(mentioned_file):
            file_creation = os.path.getmtime(mentioned_file)
            if file_creation < twodays_ago:
                os.remove(mentioned_file)
            else:
                with open(mentioned_file) as f:
                    self.mentioned = {line.strip('\n') for line in f}

    def retweet(self, tweet_id):
        return self.request("statuses/retweet/:{}".format(tweet_id))

    def tweet(self, **kwargs):
        """
        Tweet a status
        """
        endpoint = 'statuses/update'
        try:
            resp = self.request(endpoint, kwargs)
        except Exception as e:
            print('tweet:', e)
            return None
        return resp

    def auto_reply(self, screen_name, status_id):
        """
        Reply a tweet from screen_name with id status_id with a random
        status from status_file.
        """
        if screen_name not in self.mentioned:

            # can modify to use file watcher rather than opening file each time
            with open(self.status_file) as f:
                l_status = [status for status in f]

            status = '@' + screen_name + ' ' + choice(l_status)
            resp = self.tweet(status=status, in_reply_to_status_id=status_id)

            if resp:
                if 'text' in resp.text:
                    print(status, '\n')

                    # store replied users. Don't reply to the same
                    # person more than once in a session. Otherwise, spamming.
                    self.mentioned.add(screen_name)
                else:
                    print(resp.text)
            else:
                print('POST statuses/update gives no response.')


class Consumer():
    def __init__(self, SENTINEL, auth, proxies, status_file='status.txt'):
        self.SENTINEL = SENTINEL
        self.auto_reply = AutoReplier(auth, proxies, status_file)

    def start(self, recv_p):
        """consumer process the tweets from producer process and then push
        it into auto_reply
        """
        # handle SIGINT signal from parent process
        signal.signal(signal.SIGINT, self.exit_handler)

        filter_tweet = self.filter_tweet
        tweet_q = deque(maxlen=50)
        auto_reply_thread = threading.Thread(target=self.auto_reply.start,
                                             name="AutoReplier",
                                             args=(tweet_q,))
        auto_reply_thread.daemon = True
        auto_reply_thread.start()

        print('Consumer: started')
        while True:
            try:
                tweet = recv_p.recv()
            except EOFError as e:
                print('Consumer:', e)
                break

            if tweet == self.SENTINEL:
                break

            target = filter_tweet(tweet)
            if target is not None:
                tweet_q.append(target)
        self.auto_reply.save_mentioned()
        print('Consumer: ended')

    def exit_handler(self, signum, frame):
        # when exiting, save mentioned and then kill itself
        print('Process {} kills itself due to Signal {}'.format(os.getpid(),
                                                                signum))
        self.auto_reply.save_mentioned()
        os._exit(1)

    @staticmethod
    def filter_tweet(tweet):
        # input: tweet object
        # output: (screen_name, status_id)
        status_id = tweet['id']
        screen_name = tweet['user']['screen_name']
        followers_count = tweet['user']['followers_count']
        if 400 < followers_count < 3000:
            return screen_name, status_id
        else:
            return None


class Producer(TwitterAPI):
    def __init__(self, consumer_key,
                 consumer_secret,
                 access_token_key,
                 access_token_secret,
                 SENTINEL,
                 mode,
                 params_file):
        super().__init__(consumer_key,
                         consumer_secret,
                         access_token_key,
                         access_token_secret)

        self.mode = mode
        self.params_file = params_file
        self.params = None
        self.SENTINEL = SENTINEL

    def start(self, send_p):
        """
        Stream tweets and pass them through Pipes to the Consumer object.
        """
        # handle SIGINT from parent process
        signal.signal(signal.SIGINT, self.exit_handler)
        print('Producer: started')

        try:
            stream = self.request_endpoint(self.mode, self.params_file)
            if stream:
                for tweet in stream:
                    if 'created_at' in tweet:
                        send_p.send(tweet)
        except Exception as e:
            # send SENTINEL when there is an error in streaming
            send_p.send(self.SENTINEL)
            print(e)
        print('Producer: ended')

    @staticmethod
    def exit_handler(signum, frame):
        print('Process {} kills itself due to Signal {}'.format(os.getpid(),
                                                                signum))
        os._exit(1)

    def request_endpoint(self, mode, params_file):
        if mode == 'sample':
            endpoint = 'statuses/sample'
        elif mode == 'filter':
            endpoint = 'statuses/filter'
            self.parse_params(params_file)
        else:
            message = 'Mode: {} is not available!'.format(mode)
            raise Exception(message)
        return self.request(endpoint, self.params)

    def parse_params(self, params_file):
        config = configparser.ConfigParser()
        config.read(params_file)
        params = {}
        for k, v in config['params'].items():
            params[k] = [x.strip(',') for x in v.splitlines()]
        self.params = params


class TwitterBot():
    def __init__(self, mode='filter', config_file='config.ini'):
        """
        Specify the mode and then login.
        """

        config = configparser.ConfigParser()
        config.read(config_file)
        consumer_key = config['apikey']['key']
        consumer_secret = config['apikey']['secret']
        access_token_key = config['token']['key']
        access_token_secret = config['token']['secret']
        status_file = config['misc']['status_file']
        params_file = config['misc']['parameters_file']

        # check whether all the required files exist.
        required_files = [params_file, config_file, status_file]
        current_dir = os.getcwd()
        for filename in required_files:
            if not os.path.isfile(current_dir + '/' + filename):
                print('Please check whether {} exists.'.format(filename))
                sys.exit(1)

        SENTINEL = -134

        self.producer = Producer(consumer_key,
                                 consumer_secret,
                                 access_token_key,
                                 access_token_secret,
                                 SENTINEL,
                                 mode,
                                 params_file)
        self.consumer = Consumer(SENTINEL,
                                 self.producer.auth,
                                 self.producer.proxies,
                                 status_file)

    def start(self):
        print("Bot starts.")

        recv_p, send_p = Pipe(False)
        p = Process(target=self.producer.start, name="Producer",
                    args=(send_p,))
        c = Process(target=self.consumer.start, name="Consumer",
                    args=(recv_p,))
        p.daemon = True
        c.daemon = True
        p.start()
        c.start()

        # If not daemon then implicit join invoked by p and c. Will cause
        # error when getting SIGINT.
        # SIGINT in parent process will hang the child processes if not
        # handled.
        try:
            while p.is_alive() and c.is_alive():
                sleep(1.0)
        except KeyboardInterrupt:
            pass

        print('Bot stops.')
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description = 'TwitterBot v0.2',
                                     usage = "python -c your_config_file")
    parser.add_argument('-c', dest = 'config_file', default = 'config.ini')
    args = parser.parse_args()
    bot = TwitterBot(config_file = args.config_file)
    bot.start()