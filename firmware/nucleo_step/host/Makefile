# The Nucleo's firmware as a fake board on a PC (nucleo_host.cpp): the board
# layer and the core, over a fake nucleo_hw. On Windows: build_msvc.cmd.
CXX ?= c++
CXXFLAGS ?= -std=c++11 -O2 -Wall -Wextra -Wno-unused-parameter -pthread
BOARD = ../NucleoStep
CORE = ../../triggerscope_step/TriggerScopeStep

nucleo_host: nucleo_host.cpp $(BOARD)/nucleo_step.cpp $(BOARD)/nucleo_hw.h $(BOARD)/nucleo_step.h $(CORE)/step_core.cpp $(CORE)/step_core.h
	$(CXX) $(CXXFLAGS) -I$(BOARD) -I$(CORE) -o $@ nucleo_host.cpp $(BOARD)/nucleo_step.cpp

clean:
	rm -f nucleo_host

.PHONY: clean
