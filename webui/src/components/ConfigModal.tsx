import { Modal, Form, Input, InputNumber, Select, Divider } from 'antd';
import type { AppConfig } from '../types';

interface Props {
  open: boolean;
  config: AppConfig | null;
  onCancel: () => void;
  onSave: (config: AppConfig) => Promise<void>;
}

const HELP: Record<string, string> = {
  kp: '比例增益。误差每1℃加Kp%PWM，越大响应越快但易振荡',
  ki: '积分增益。消除稳态误差(温度长期偏离的累积修正)，过大会振荡',
  kd: '微分增益。抑制突变预判趋势，温度噪声大时易放大干扰，默认关',
  load_kf: 'CPU负载前馈增益。CPU一忙就提前加速风扇不等温度升。0.3=80%负载加24%PWM',
  delta_t_k: '进排风温差前馈增益。温差大说明整机热负荷高(含硬盘/显卡)，超10℃基准才加成',
};

/** 配置编辑弹窗：IPMI 连接 / 控制参数 / PID / 前馈 / 日志 */
export function ConfigModal({ open, config, onCancel, onSave }: Props) {
  const [form] = Form.useForm<AppConfig>();

  return (
    <Modal
      title="编辑配置"
      open={open}
      onCancel={onCancel}
      okText="保存到文件"
      cancelText="取消"
      width={560}
      destroyOnClose
      onOk={async () => {
        const values = await form.validateFields();
        await onSave(values);
      }}
    >
      {config && (
        <Form
          form={form}
          layout="horizontal"
          labelCol={{ flex: '88px' }}
          wrapperCol={{ flex: 'auto' }}
          initialValues={config}
        >
          <Divider orientation="left" plain>
            IPMI 连接
          </Divider>
          <Form.Item name="ipmi_mode" label="IPMI 模式">
            <Select
              options={[
                { value: 'network', label: '网络(pyghmi)' },
                { value: 'local', label: '本机(ipmitool)' },
              ]}
            />
          </Form.Item>
          <Form.Item name="ip" label="BMC IP">
            <Input />
          </Form.Item>
          <Form.Item name="user" label="用户名">
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码">
            <Input.Password placeholder="明文或 ${DELL_BMC_PASSWORD}" />
          </Form.Item>

          <Divider orientation="left" plain>
            控制参数
          </Divider>
          <Form.Item name="target_cpu_temp" label="目标温度">
            <InputNumber step={1} addonAfter="℃" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="emergency_temp" label="紧急温度">
            <InputNumber step={1} addonAfter="℃" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="inlet_safe_max" label="进风上限">
            <InputNumber step={1} addonAfter="℃" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="interval" label="采样间隔">
            <InputNumber step={0.5} addonAfter="秒" style={{ width: '100%' }} />
          </Form.Item>

          <Divider orientation="left" plain>
            PID 参数
          </Divider>
          <Form.Item name="kp" label="Kp" tooltip={HELP.kp}>
            <InputNumber step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="ki" label="Ki" tooltip={HELP.ki}>
            <InputNumber step={0.05} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="kd" label="Kd" tooltip={HELP.kd}>
            <InputNumber step={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="pwm_min" label="PWM 下限">
            <InputNumber step={1} addonAfter="%" style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="pwm_max" label="PWM 上限">
            <InputNumber step={1} addonAfter="%" style={{ width: '100%' }} />
          </Form.Item>

          <Divider orientation="left" plain>
            前馈参数
          </Divider>
          <Form.Item name="load_kf" label="负载前馈" tooltip={HELP.load_kf}>
            <InputNumber step={0.05} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="delta_t_k" label="温差前馈" tooltip={HELP.delta_t_k}>
            <InputNumber step={0.1} style={{ width: '100%' }} />
          </Form.Item>

          <Divider orientation="left" plain>
            日志
          </Divider>
          <Form.Item name="log_level" label="日志级别">
            <Select
              options={['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((v) => ({
                value: v,
              }))}
            />
          </Form.Item>
          <Form.Item name="log_file" label="日志文件">
            <Input />
          </Form.Item>
        </Form>
      )}
    </Modal>
  );
}
