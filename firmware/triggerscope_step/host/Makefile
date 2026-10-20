# The firmware core as a fake board on a PC (host_board.cpp).
CXX ?= c++
CXXFLAGS ?= -std=c++11 -O2 -Wall -Wextra -Wno-unused-parameter
CORE = ../TriggerScopeStep

host_board: host_board.cpp $(CORE)/step_core.cpp $(CORE)/step_core.h
	$(CXX) $(CXXFLAGS) -I$(CORE) -o $@ host_board.cpp $(CORE)/step_core.cpp

clean:
	rm -f host_board

.PHONY: clean
