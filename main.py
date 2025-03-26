"""
设备配置加载器
用于将YAML格式的设备配置文件导入到PostgreSQL数据库中

Features:
- 支持设备基本信息、连接参数和属性的配置
- 自动创建和更新数据库表结构
- 完整的错误处理和日志记录
- 支持增量更新配置
"""

import yaml
import psycopg2
import json
from datetime import datetime
from typing import Dict, Any
import logging
import argparse
from pathlib import Path
import os

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def clean_dict(d: Dict) -> Dict:
    """递归清理字典中所有的字符串值和键的空格"""
    if not isinstance(d, dict):
        return d.strip() if isinstance(d, str) else d
        
    return {
        k.strip() if isinstance(k, str) else k: 
        clean_dict(v) if isinstance(v, dict) else (
            v.strip() if isinstance(v, str) else v
        )
        for k, v in d.items()
    }


class DeviceConfigLoader:
    """设备配置加载器类"""
    
    def __init__(self, db_params: Dict[str, str]):
        """
        初始化设备配置加载器
        
        Args:
            db_params: 数据库连接参数字典
            {
                'dbname': str,
                'user': str,
                'password': str,
                'host': str,
                'port': str
            }
        """
        self.db_params = db_params
        self.conn = None
        self.cur = None

    def connect_db(self):
        """建立数据库连接"""
        try:
            self.conn = psycopg2.connect(**self.db_params)
            self.cur = self.conn.cursor()
            logger.info("数据库连接成功")
        except Exception as e:
            logger.error(f"数据库连接失败: {str(e)}")
            raise

    def close_db(self):
        """关闭数据库连接"""
        if self.cur:
            self.cur.close()
        if self.conn:
            self.conn.close()
            logger.info("数据库连接已关闭")

    def check_tables_exist(self) -> bool:
        """
        检查必要的表是否存在
        
        Returns:
            bool: 所有必要的表是否都存在
        """
        try:
            self.cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'devices'
                ) AND EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'device_properties'
                ) AND EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'device_connection_params'
                );
            """)
            return self.cur.fetchone()[0]
        except Exception as e:
            logger.error(f"检查表存在时发生错误: {str(e)}")
            return False

    def init_database(self, force: bool = False):
        """
        初始化数据库表结构
        
        Args:
            force: 是否强制重新创建表结构
        """
        try:
            if force:
                self.cur.execute("""
                    DROP TABLE IF EXISTS device_properties CASCADE;
                    DROP TABLE IF EXISTS device_connection_params CASCADE;
                    DROP TABLE IF EXISTS devices CASCADE;
                """)
                logger.warning("已删除现有表结构")

            # 创建设备基本信息表
            self.cur.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(100),
                    device_type VARCHAR(50),
                    system VARCHAR(50),
                    location VARCHAR(50),
                    communication_type VARCHAR(20) NOT NULL,
                    communication_id VARCHAR(50) NOT NULL,
                    connection_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','ready')),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status);
                             
                CREATE INDEX IF NOT EXISTS idx_devices_communication_type 
                ON devices(communication_type);
                
                CREATE INDEX IF NOT EXISTS idx_devices_connection_type 
                ON devices(connection_type);
            """)

            # 创建连接参数表
            self.cur.execute("""
                CREATE TABLE IF NOT EXISTS device_connection_params (
                    device_id VARCHAR(50) PRIMARY KEY 
                    REFERENCES devices(device_id) ON DELETE CASCADE,
                    port_number VARCHAR(10) NOT NULL,
                    slave_address VARCHAR(10) NOT NULL,
                    baud_rate VARCHAR(10) DEFAULT '9600',
                    stop_bits INTEGER DEFAULT 1,
                    data_bits INTEGER DEFAULT 8,
                    parity VARCHAR(10) DEFAULT 'None',
                    timeout FLOAT DEFAULT 3.0,
                    retry_count INTEGER DEFAULT 3,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)

            # 创建设备属性表
            self.cur.execute("""
                CREATE TABLE IF NOT EXISTS device_properties (
                    id SERIAL PRIMARY KEY,
                    device_id VARCHAR(50) 
                    REFERENCES devices(device_id) ON DELETE CASCADE,
                    property_id VARCHAR(50) NOT NULL,
                    request_type VARCHAR(20) NOT NULL,
                    description TEXT,
                    function_code VARCHAR(10) NOT NULL,
                    register_address VARCHAR(10) NOT NULL,
                    data_type VARCHAR(20) NOT NULL,
                    data_length INTEGER NOT NULL,
                    byte_order VARCHAR(20) DEFAULT 'big_endian',
                    word_order VARCHAR(20) DEFAULT 'big_endian',
                    scale_factor FLOAT DEFAULT 1.0,
                    value_offset FLOAT DEFAULT 0.0,
                    enum_values JSONB,
                    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'success', 'failed')),
                    session_id VARCHAR(50),
                    retry_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(device_id, property_id)
                );

                CREATE INDEX IF NOT EXISTS idx_device_properties_device_id 
                ON device_properties(device_id);
                CREATE INDEX IF NOT EXISTS idx_device_properties_status
                ON device_properties(status);
                CREATE INDEX IF NOT EXISTS idx_device_properties_session_id
                ON device_properties(session_id);
            """)

            # 创建更新时间触发器
            self.cur.execute("""
                CREATE OR REPLACE FUNCTION update_updated_at_column()
                RETURNS TRIGGER AS $$
                BEGIN
                    NEW.updated_at = CURRENT_TIMESTAMP;
                    RETURN NEW;
                END;
                $$ language 'plpgsql';

                DROP TRIGGER IF EXISTS update_devices_timestamp ON devices;
                CREATE TRIGGER update_devices_timestamp
                    BEFORE UPDATE ON devices
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at_column();

                DROP TRIGGER IF EXISTS update_connection_params_timestamp 
                ON device_connection_params;
                CREATE TRIGGER update_connection_params_timestamp
                    BEFORE UPDATE ON device_connection_params
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at_column();

                DROP TRIGGER IF EXISTS update_properties_timestamp 
                ON device_properties;
                CREATE TRIGGER update_properties_timestamp
                    BEFORE UPDATE ON device_properties
                    FOR EACH ROW
                    EXECUTE FUNCTION update_updated_at_column();
            """)

            self.conn.commit()
            logger.info("数据库表初始化成功")
            
        except Exception as e:
            self.conn.rollback()
            logger.error(f"数据库表初始化失败: {str(e)}")
            raise

    def load_yaml(self, yaml_file: str) -> Dict[str, Any]:
        """
        加载YAML配置文件
        
        Args:
            yaml_file: YAML文件路径
            
        Returns:
            Dict[str, Any]: 加载的配置数据
        """
        try:
            yaml_path = Path(yaml_file)
            if not yaml_path.exists():
                raise FileNotFoundError(f"配置文件不存在: {yaml_file}")
                
            with open(yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                return clean_dict(data)  # 使用新的清理函数
        except Exception as e:
            logger.error(f"YAML文件加载失败: {str(e)}")
            raise

    def update_device(self, device_name: str, device_data: Dict[str, Any]):
        """
        更新或插入设备信息
        
        Args:
            device_name: 设备名称
            device_data: 设备配置数据
        """
        try:
            # 1. 插入设备基本信息
            device = {
                'device_id': device_data['device_id'],
                'name': device_data.get('name'),
                'device_type': device_data.get('device_type'),
                'system': device_data.get('system'),
                'location': device_data.get('location'),
                'communication_type': device_data['communication_type'],
                'communication_id': device_data['communication_id'],
                'connection_type': device_data['connection_type']
            }

            self.cur.execute("""
                INSERT INTO devices (
                    device_id, name, device_type, system, location,
                    communication_type, communication_id, connection_type
                ) VALUES (
                    %(device_id)s, %(name)s, %(device_type)s, %(system)s, 
                    %(location)s, %(communication_type)s, %(communication_id)s, 
                    %(connection_type)s
                )
                ON CONFLICT (device_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    device_type = EXCLUDED.device_type,
                    system = EXCLUDED.system,
                    location = EXCLUDED.location,
                    communication_type = EXCLUDED.communication_type,
                    communication_id = EXCLUDED.communication_id,
                    connection_type = EXCLUDED.connection_type
            """, device)

            # 2. 插入连接参数
            conn_params = device_data['connection_params']
            conn_params['device_id'] = device_data['device_id']
            
            # 设置默认值
            conn_params.setdefault('baud_rate', '9600')
            conn_params.setdefault('stop_bits', 1)
            conn_params.setdefault('data_bits', 8)
            conn_params.setdefault('parity', 'None')
            conn_params.setdefault('timeout', 3.0)
            conn_params.setdefault('retry_count', 3)
            
            self.cur.execute("""
                INSERT INTO device_connection_params (
                    device_id, port_number, slave_address, baud_rate,
                    stop_bits, data_bits, parity, timeout, retry_count
                ) VALUES (
                    %(device_id)s, %(port_number)s, %(slave_address)s, 
                    %(baud_rate)s, %(stop_bits)s, %(data_bits)s, 
                    %(parity)s, %(timeout)s, %(retry_count)s
                )
                ON CONFLICT (device_id) DO UPDATE SET
                    port_number = EXCLUDED.port_number,
                    slave_address = EXCLUDED.slave_address,
                    baud_rate = EXCLUDED.baud_rate,
                    stop_bits = EXCLUDED.stop_bits,
                    data_bits = EXCLUDED.data_bits,
                    parity = EXCLUDED.parity,
                    timeout = EXCLUDED.timeout,
                    retry_count = EXCLUDED.retry_count
            """, conn_params)

            # 3. 更新设备属性
            if 'properties' in device_data:
                self.update_device_properties(
                    device_data['device_id'], 
                    device_data['properties']
                )

            logger.info(f"设备 {device_data['device_id']} 更新成功")

        except Exception as e:
            logger.error(f"设备 {device_name} 更新失败: {str(e)}")
            raise

    def update_device_properties(self, device_id: str, 
                                properties: Dict[str, Any]):
        """
        更新或插入设备属性
        
        Args:
            device_id: 设备ID
            properties: 属性配置数据
        """
        for prop_name, prop_data in properties.items():
            try:
                # 准备属性数据
                property_data = {
                    'device_id': device_id,
                    'property_id': prop_data['property_id'],
                    'request_type': prop_data['request_type'],
                    'description': prop_data.get('description'),
                    'function_code': prop_data['command']['function_code'],
                    'register_address': prop_data['command']['register_address'],
                    'data_type': prop_data['parse_method']['data_type'],
                    'data_length': prop_data['parse_method']['data_length'],
                    'byte_order': prop_data['parse_method'].get('byte_order', 
                                                            'big_endian'),
                    'word_order': prop_data['parse_method'].get('word_order', 
                                                            'big_endian'),
                    'scale_factor': prop_data['parse_method'].get('scale_factor', 
                                                                1.0),
                    'value_offset': prop_data['parse_method'].get('offset', 0.0),
                    'enum_values': json.dumps(
                        prop_data['parse_method'].get('enum_values'))
                    if 'enum_values' in prop_data['parse_method'] else None
                }

                self.cur.execute("""
                    INSERT INTO device_properties (
                        device_id, property_id, request_type, description,
                        function_code, register_address, data_type, data_length,
                        byte_order, word_order, scale_factor, value_offset, 
                        enum_values
                    ) VALUES (
                        %(device_id)s, %(property_id)s, %(request_type)s, 
                        %(description)s, %(function_code)s, 
                        %(register_address)s, %(data_type)s, %(data_length)s,
                        %(byte_order)s, %(word_order)s, %(scale_factor)s, 
                        %(value_offset)s, %(enum_values)s
                    )
                    ON CONFLICT (device_id, property_id) DO UPDATE SET
                        request_type = EXCLUDED.request_type,
                        description = EXCLUDED.description,
                        function_code = EXCLUDED.function_code,
                        register_address = EXCLUDED.register_address,
                        data_type = EXCLUDED.data_type,
                        data_length = EXCLUDED.data_length,
                        byte_order = EXCLUDED.byte_order,
                        word_order = EXCLUDED.word_order,
                        scale_factor = EXCLUDED.scale_factor,
                        value_offset = EXCLUDED.value_offset,
                        enum_values = EXCLUDED.enum_values
                """, property_data)

                logger.info(f"属性 {prop_data['property_id']} 更新成功")

            except Exception as e:
                logger.error(f"属性 {prop_name} 更新失败: {str(e)}")
                raise

    def process_config(self, yaml_file: str, init_db: bool = False):
        """
        处理配置文件的主函数
        
        Args:
            yaml_file: YAML配置文件路径
            init_db: 是否初始化数据库
        """
        try:
            self.connect_db()
            
            # 初始化数据库时保持 autocommit 模式
            if init_db or not self.check_tables_exist():
                if not self.check_tables_exist():
                    logger.info("数据库表不存在，进行初始化")
                self.init_database(force=init_db)
                
            config_data = self.load_yaml(yaml_file)
            
            # 设置 autocommit 为 False 之前，确保没有活动的事务
            self.conn.commit()
            self.conn.autocommit = False
            
            try:
                for device_name, device_data in config_data.items():
                    self.update_device(device_name, device_data)
                
                self.conn.commit()
                logger.info("配置更新完成")
                
            except Exception as e:
                self.conn.rollback()
                raise
                
        except Exception as e:
            logger.error(f"配置处理失败: {str(e)}")
            raise
        finally:
            self.close_db()

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='设备配置管理工具')
    parser.add_argument('--yaml', required=True, help='YAML配置文件路径')
    parser.add_argument('--init-db', action='store_true', 
                        help='初始化数据库（如果表结构改变，使用此选项）')
    parser.add_argument('--db-host', default='localhost', help='数据库主机地址')
    parser.add_argument('--db-port', default='5432', help='数据库端口')
    parser.add_argument('--db-name', default='modbus_device', help='数据库名称')
    parser.add_argument('--db-user', help='数据库用户名')
    parser.add_argument('--db-password', help='数据库密码')
    args = parser.parse_args()

    # 优先从环境变量获取数据库配置
    db_params = {
        'dbname': os.getenv('DB_NAME', args.db_name),
        'user': os.getenv('DB_USER', args.db_user),
        'password': os.getenv('DB_PASSWORD', args.db_password),
        'host': os.getenv('DB_HOST', args.db_host),
        'port': os.getenv('DB_PORT', args.db_port)
    }

    # 检查必要的数据库参数
    required_params = ['user', 'password']
    missing_params = [param for param in required_params if not db_params[param]]
    if missing_params:
        logger.error(f"缺少必要的数据库配置参数: {', '.join(missing_params)}")
        logger.info("请通过环境变量或命令行参数提供数据库配置")
        exit(1)

    try:
        loader = DeviceConfigLoader(db_params)
        loader.process_config(args.yaml, init_db=args.init_db)
    except Exception as e:
        logger.error(f"程序执行失败: {str(e)}")
        exit(1)

if __name__ == "__main__":
    main()