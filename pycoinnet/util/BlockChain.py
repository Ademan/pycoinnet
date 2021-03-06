import logging
import weakref

from pycoinnet.util.ChainFinder import ChainFinder
from pycoinnet.util.Queue import Queue

ZERO_HASH = b'\0' * 32


def _update_q(q, ops):
    # first, we meld out complimentary adds and removes
    while len(ops) > 0:
        op = ops[0]
        if op[0] != 'remove':
            break
        last = q.pop()
        if op[1:] != last[1:]:
            q.put_nowait(last)
            break
        ops = ops[1:]
    for op in ops:
        q.put_nowait(op)


class BlockChain:
    def __init__(self, parent_hash=ZERO_HASH, did_lock_to_index_f=None):
        self.parent_hash = parent_hash
        self.hash_to_index_lookup = {}
        self.weight_lookup = {}
        self.chain_finder = ChainFinder()
        self.change_queues = set() #weakref.WeakSet()
        self._longest_chain_cache = None
        self.did_lock_to_index_f = did_lock_to_index_f

        self._locked_chain = []

    def is_hash_known(the_hash):
        return the_hash in self.hash_to_index_lookup

    def length(self):
        return len(self._longest_local_block_chain()) + len(self._locked_chain)

    def locked_length(self):
        return len(self._locked_chain)

    def tuple_for_index(self, index):
        if index < 0:
            index = self.length() + index
        l = len(self._locked_chain)
        if index < l:
            return self._locked_chain[index]
        index -= l

        longest_chain = self._longest_local_block_chain()
        the_hash = longest_chain[-index-1]
        parent_hash = self.parent_hash if index <= 0 else self._longest_chain_cache[-index]
        weight = self.weight_lookup.get(the_hash)
        return (the_hash, parent_hash, weight)

    def last_block_hash(self):
        if self.length() == 0:
            return self.parent_hash
        return self.hash_for_index(-1)

    def hash_for_index(self, index):
        return self.tuple_for_index(index)[0]

    def index_for_hash(self, the_hash):
        return self.hash_to_index_lookup.get(the_hash)

    def new_change_q(self):
        q = Queue()
        self.change_queues.add(q)
        return q

    def lock_to_index(self, index):
        old_length = len(self._locked_chain)
        index -= old_length
        longest_chain = self._longest_local_block_chain()
        if index < 1:
            return
        excluded = set()
        for idx in range(index):
            the_hash = longest_chain[-idx-1]
            parent_hash = self.parent_hash if idx <= 0 else self._longest_chain_cache[-idx]
            weight = self.weight_lookup.get(the_hash)
            item = (the_hash, parent_hash, weight)
            self._locked_chain.append(item)
            excluded.add(the_hash)
        if self.did_lock_to_index_f:
            self.did_lock_to_index_f(self._locked_chain[old_length:old_length+index], old_length)
        old_chain_finder = self.chain_finder
        self.chain_finder = ChainFinder()
        self._longest_chain_cache = None

        def iterate():
            for tree in old_chain_finder.trees_from_bottom.values():
                for c in tree:
                    if c in excluded:
                        break
                    excluded.add(c)
                    yield (c, old_chain_finder.parent_lookup[c])
        self.chain_finder.load_nodes(iterate())
        self.parent_hash = the_hash

    def _longest_local_block_chain(self):
        if self._longest_chain_cache is None:
            max_weight = 0
            longest = []
            for chain in self.chain_finder.all_chains_ending_at(self.parent_hash):
                weight = sum(self.weight_lookup.get(h, 0) for h in chain)
                if weight > max_weight:
                    longest = chain
                    max_weight = weight
            self._longest_chain_cache = longest[:-1]
        return self._longest_chain_cache

    def add_headers(self, header_iter):
        def hash_parent_weight_tuples():
            for h in header_iter:
                yield h.hash(), h.previous_block_hash, h.difficulty

        return self.add_nodes(hash_parent_weight_tuples())

    def add_nodes(self, hash_parent_weight_tuples):
        def iterate():
            for h, p, w in hash_parent_weight_tuples:
                self.weight_lookup[h] = w
                yield h, p

        old_longest_chain = self._longest_local_block_chain()

        self.chain_finder.load_nodes(iterate())

        self._longest_chain_cache = None
        new_longest_chain = self._longest_local_block_chain()

        if old_longest_chain and new_longest_chain:
            old_path, new_path = self.chain_finder.find_ancestral_path(
                old_longest_chain[0],
                new_longest_chain[0]
            )
            old_path = old_path[:-1]
            new_path = new_path[:-1]
        else:
            old_path = old_longest_chain
            new_path = new_longest_chain
        if old_path:
            logging.debug("old_path is %s-%s", old_path[0], old_path[-1])
        if new_path:
            logging.debug("new_path is %s-%s", new_path[0], new_path[-1])
            logging.debug("block chain now has %d elements", self.length())

        # return a list of operations:
        # ("add"/"remove", the_hash, the_index)
        ops = []
        size = len(old_longest_chain) + len(self._locked_chain)
        for idx, h in enumerate(old_path):
            op = ("remove", h, size-idx-1)
            ops.append(op)
            del self.hash_to_index_lookup[size-idx-1]
        size = len(new_longest_chain) + len(self._locked_chain)
        for idx, h in reversed(list(enumerate(new_path))):
            op = ("add", h, size-idx-1)
            ops.append(op)
            self.hash_to_index_lookup[size-idx-1] = h
        for q in self.change_queues:
            _update_q(q, ops)

        return ops
