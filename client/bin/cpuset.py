__author__ = """Copyright Google, Peter Dahl, Martin J. Bligh   2007"""

import os, sys, re, glob, math
from autotest_utils import *

# Convert '1-3,7,9-12' to [1,2,3,7,9,10,11,12]
def rangelist_to_list(rangelist):
	result = []
	if not rangelist:
		return result
	for x in rangelist.split(','):
		if re.match(r'^(\d+)$', x):
			result.append(int(x))
			continue
		m = re.match(r'^(\d+)-(\d+)$', x)
		if m:
			start = int(m.group(1))
			end = int(m.group(2))
			result += range(start, end+1)
			continue
		msg = 'Cannot understand data input: %s %s' % (x, rangelist)
		raise ValueError(msg)
	return result


def rounded_memtotal():
	# Get total of all physical mem, in Kbytes
	usable_Kbytes = memtotal()
	# usable_Kbytes is system's usable DRAM in Kbytes,
	#   as reported by memtotal() from device /proc/meminfo memtotal
	#   after Linux deducts 1.5% to 5.1% for system table overhead
	# Undo the unknown actual deduction by rounding up
	#   to next small multiple of a big power-of-two
	#   eg  12GB - 5.1% gets rounded back up to 12GB
	mindeduct = 0.015  # 1.5 percent
	maxdeduct = 0.055  # 5.5 percent
	# deduction range 1.5% .. 5.5% supports physical mem sizes
	#    6GB .. 12GB in steps of .5GB
	#   12GB .. 24GB in steps of 1 GB
	#   24GB .. 48GB in steps of 2 GB ...
	# Finer granularity in physical mem sizes would require
	#   tighter spread between min and max possible deductions

	# increase mem size by at least min deduction, without rounding
	min_Kbytes   = int(usable_Kbytes / (1.0 - mindeduct))
	# increase mem size further by 2**n rounding, by 0..roundKb or more
	round_Kbytes = int(usable_Kbytes / (1.0 - maxdeduct)) - min_Kbytes
	# find least binary roundup 2**n that covers worst-cast roundKb
	mod2n = 1 << int(math.ceil(math.log(round_Kbytes, 2)))
	# have round_Kbytes <= mod2n < round_Kbytes*2
	# round min_Kbytes up to next multiple of mod2n
	phys_Kbytes = min_Kbytes + mod2n - 1
	phys_Kbytes = phys_Kbytes - (phys_Kbytes % mod2n)  # clear low bits
	return phys_Kbytes


def my_container_name():
	# Get current process's inherited or self-built container name
	#   within /dev/cpuset.  Is '/' for root container, '/sys', etc. 
	return read_one_line('/proc/%i/cpuset' % os.getpid())


def my_mem_nodes():
	# Get list of numa memory nodes owned by current process's container.
	mems = read_one_line('/dev/cpuset%s/mems' % my_container_name())
	return rangelist_to_list(mems)


def mbytes_per_mem_node():
	# Get mbyte size of each numa mem node, as float
	# Replaces autotest_utils.node_size().
	# Based on guessed total physical mem size, not on kernel's 
	#   lesser 'available memory' after various system tables.
	# Can be non-integer when kernel sets up 15 nodes instead of 16.
	return rounded_memtotal() / (len(numa_nodes()) * 1024.0)


def get_tasks(setname):
	return [x.rstrip() for x in open(setname+'/tasks').readlines()]


def print_one_cpuset(name):
	dir = os.path.join('/dev/cpuset', name)
	cpus = read_one_line(dir + '/cpus')
	mems = read_one_line(dir + '/mems')
	node_size_ = rounded_memtotal()*1024 / len(numa_nodes())
	memtotal = node_size_ * len(rangelist_to_list(mems))
	tasks = ','.join(get_tasks(dir))
	print "cpuset %s: size %s; tasks %s; cpus %s; mems %s" % \
	      (name, human_format(memtotal), tasks, cpus, mems)


def print_all_cpusets():
	for cpuset in glob.glob('/dev/cpuset/*'):
		print_one_cpuset(re.sub(r'.*/', '', cpuset))


def get_mems(setname):
	file_name = os.path.join(setname, "mems")
	if os.path.exists(file_name):
		return rangelist_to_list(read_one_line(file_name))
	else:
		return ""


class cpuset:
	super_root = "/dev/cpuset"


	def display(self):
		print_one_cpuset(os.path.join(self.root, self.name))


	# Start with the nodes available one level up in the cpuset tree,
	#   subtract off nodes of all siblings at this level.
	def available_mems(self, parent_nodes):
		available = set(parent_nodes)
		for sub_cpusets in glob.glob('%s/*/mems' % self.root):
			sub_cpusets = os.path.dirname(sub_cpusets)
			available -= set(get_mems(sub_cpusets))
		return list(available)

	def release(self):
		# don't do anything if this is a top-level dir
		if self.root == self.cpudir:
			return
		print "releasing ", self.cpudir
		parent_t = os.path.join(self.root, 'tasks')
		# Transfer survivors (and self) to parent
		for task in get_tasks(self.cpudir):
			write_one_line(parent_t, task)
		os.rmdir(self.cpudir)
		if os.path.exists(self.cpudir):
			raise AutotestError('Could not delete container ' 
						+ self.cpudir)


	def __init__(self, name, job_size=None, job_pid=None, cpus=None,
		     root=""):
		"""\
		Create a cpuset container and move job_pid into it
		Allocate the list "cpus" of cpus to that container

			name = arbitrary string tag
			job_size = reqested memory for job in megabytes
			job_pid = pid of job we're putting into the container
			cpu = list of cpu indicies to associate with the cpuset
			root = the cpuset to create this new set in
		"""
		self.root = os.path.join(self.super_root, root)
		self.name = name

		# default to all installed memory
		memtotal_bytes = rounded_memtotal() << 10
		if not job_size:
			job_size = memtotal_bytes >> 20

		# default to the current pid
		if not job_pid:
			job_pid = os.getpid()

		print "cpuset(name=%s, root=%s, job_size=%d, pid=%d)" % \
		    (name, root, job_size, job_pid)
		self.memory = job_size
		# Convert jobsize to bytes
		job_size = job_size << 20
		if not grep('cpuset', '/proc/filesystems'):
			raise AutotestError('No cpuset support; please reboot')
		if not os.path.exists(self.super_root):
			os.mkdir(self.super_root)
			system('mount -t cpuset none %s' % self.super_root)
		if not os.path.exists(os.path.join(self.super_root, "cpus")):
			raise AutotestError('Root container /dev/cpuset is '
						'empty; please reboot')
		if not os.path.exists(self.root):
			raise AutotestError('Parent container %s does not exist'
						 % self.root)
		if cpus == None:
			cpus = range(count_cpus())
		self.cpus = cpus
		all_nodes = numa_nodes()

		self.cpudir = os.path.join(self.root, name)
		if os.path.exists(self.cpudir):
			self.release()   # destructively replace old

		node_size = float(memtotal_bytes) / len(all_nodes)
		nodes_needed = int(math.ceil(float(job_size) /
					     math.ceil(node_size)))

		if nodes_needed > len(all_nodes):
			raise AutotestError("Container's memory is bigger "
						"than entire machine")
		parent_nodes = get_mems(self.root)
		if nodes_needed > len(parent_nodes):
			raise AutotestError("Container's memory is bigger "
						"than parent's")

		while True:
			# Pick specific free mem nodes for this cpuset
			mems = self.available_mems(parent_nodes)
			if len(mems) < nodes_needed:
				raise AutotestError('Existing containers hold '
					'mem nodes needed by new container')
			mems = mems[-nodes_needed:]
			mems_spec = ','.join(['%d' % x for x in mems])
			os.mkdir(self.cpudir)
			write_one_line(os.path.join(self.cpudir,
					'mem_exclusive'), '1')
			write_one_line(os.path.join(self.cpudir,'mems'), 
					mems_spec)
			# Above sends err msg to client.log.0, but no exception,
			#   if mems_spec contained any now-taken nodes
			# Confirm that siblings didn't grab our chosen mems:
			nodes_gotten = len(get_mems(self.cpudir))
			if nodes_gotten >= nodes_needed:
				break   # success
			print "cpuset %s lost race for nodes" % name, mems_spec
			# Return any mem we did get, and try again
			os.rmdir(self.cpudir)

		# add specified cpu cores and own task pid to container:
		cpu_spec = ','.join(['%d' % x for x in cpus])
		write_one_line(os.path.join(self.cpudir, 'cpus'), cpu_spec)
		write_one_line(os.path.join(self.cpudir, 'tasks'), 
				"%d" % job_pid)
		self.display()
