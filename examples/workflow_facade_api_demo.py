#!/usr/bin/env python3
"""Demonstrates using api.workflowFacade.build() to construct facades.

This example shows the new clean API for building MicroscopeFacade objects
without needing to import internal modules or access api._master.

Prerequisites:
- ImSwitch running with a valid hardware configuration
- API server enabled (pyroServerInfo.active=true in setup JSON)

Usage:
    python examples/workflow_facade_api_demo.py
"""

import Pyro5.api


def main():
    # Connect to ImSwitch API server
    print("Connecting to ImSwitch API...")
    api = Pyro5.api.Proxy('PYRO:imswitch@localhost:8001')
    
    print("\n=== WorkflowFacadeController Demo ===\n")
    
    # Example 1: Build facade with laser aliases
    print("1. Building facade with laser aliases...")
    try:
        facade = api.workflowFacade.build(
            laser_aliases={'488': 'Laser488', '405': 'Laser405'},
            detector_name='Kiralux',
            z_stage_name='Z-Piezo'
        )
        print("   ✓ Facade created successfully")
        print(f"   - Type: {type(facade)}")
        print(f"   - Has laser_con: {hasattr(facade, 'laser_con')}")
        print(f"   - Has cam: {hasattr(facade, 'cam')}")
        print(f"   - Has z_stage_con: {hasattr(facade, 'z_stage_con')}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
    
    # Example 2: Build minimal facade (no arguments)
    print("\n2. Building minimal facade (no arguments)...")
    try:
        minimal_facade = api.workflowFacade.build()
        print("   ✓ Minimal facade created successfully")
        print(f"   - Type: {type(minimal_facade)}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
    
    # Example 3: Use facade in a simple workflow
    print("\n3. Using facade to control laser power...")
    try:
        # Set laser power (safe, doesn't enable laser)
        facade.laser_con.set_constant_power('488', 10.0)
        print("   ✓ Set 488 laser power to 10.0 mW")
        
        # Query current power
        power = facade.laser_con.get_power('488')
        print(f"   ✓ Current 488 laser power: {power} mW")
    except Exception as e:
        print(f"   ✗ Error: {e}")
    
    print("\n=== Demo Complete ===\n")
    print("Key benefits of api.workflowFacade.build():")
    print("  • No need to import internal facade modules")
    print("  • No need to access api._master")
    print("  • Clean, documented API")
    print("  • Easy to discover and use")


if __name__ == '__main__':
    main()
